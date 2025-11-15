#!/usr/bin/env python3
"""
Hive Telegram Signal — Updated with BNB price included.

Contains:
 - HMI scoring display
 - BTC vs ALT dominance (ETH+BNB+SOL)
 - Token prices: BTC, ETH, BNB, SOL
 - Suggested allocation with yesterday → today moves
 - Action showing % of BTC (or ALTs) to rotate
 - Hive ROI vs BTC Buy & Hold
 - Hive trade count
"""

import os
import time
from datetime import datetime
import requests
import pandas as pd
import numpy as np

# ---------------- CONFIG ----------------

COINBASE_BASE  = "https://api.exchange.coinbase.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

HMI_STABLE_THRESHOLD = 77.0

DOM_LOW  = 0.75
DOM_HIGH = 0.81
DOM_MID_LOW  = 0.771
DOM_MID_HIGH = 0.789

ALT_IDS = ["ethereum", "binancecoin", "solana"]

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

STARTING_BAL = 100.0
BAL_FILE = "output/hive_balance.csv"
ALLOC_FILE = "output/hive_allocations.csv"


# ---------------- HELPERS ----------------

def cb_get(path, params=None, sleep=0.15):
    if params is None:
        params = {}
    r = requests.get(COINBASE_BASE + path, params=params, timeout=20)
    r.raise_for_status()
    time.sleep(sleep)
    return r.json()


def cg_get(path, params=None, sleep=0.2):
    if params is None:
        params = {}
    r = requests.get(COINGECKO_BASE + path, params=params, timeout=30)
    r.raise_for_status()
    time.sleep(sleep)
    return r.json()


def load_latest_hmi(path="output/fg2_daily.csv"):
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date")
    row = df.iloc[-1]
    return float(row["FG_lite"]), row["date"].date()


def hmi_band(f):
    if f < 10:
        return "zombie apocalypse", "🔴"
    elif f < 25:
        return "mcdonalds applications", "🟠"
    elif f < 35:
        return "ngmi", "🟡"
    elif f < 65:
        return "stable", "⚪"
    elif f < 75:
        return "we’re early", "🟢"
    else:
        return "the future of finance", "🟢🟢"


# ---------------- DOMINANCE ----------------

def fetch_btc_dom():
    js = cg_get(
        "/coins/markets",
        params={"vs_currency": "usd", "ids": "bitcoin," + ",".join(ALT_IDS)}
    )
    caps = {row["id"]: row["market_cap"] for row in js}

    btc = caps["bitcoin"]
    alts = caps["ethereum"] + caps["binancecoin"] + caps["solana"]
    if btc + alts == 0:
        return 0.5
    return btc / (btc + alts)


# ---------------- PRICES ----------------

def fetch_prices():
    out = {}
    for sym in ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD"]:
        try:
            js = cb_get(f"/products/{sym}/ticker")
            coin = sym.split("-")[0]
            out[coin] = float(js["price"])
        except:
            out[coin] = None
    return out


# ---------------- ALLOCATION ----------------

def allocation_from_dom_and_hmi(dom, hmi):
    # Extreme greed → stables
    if hmi >= HMI_STABLE_THRESHOLD:
        return {"btc": 0, "alts": 0, "stables": 1}

    # Mid dominance → stables
    if DOM_MID_LOW <= dom <= DOM_MID_HIGH:
        return {"btc": 0, "alts": 0, "stables": 1}

    # LOW DOM REGION: 0.75 → 0.77
    if dom < DOM_MID_LOW:
        frac = (dom - DOM_LOW) / (DOM_MID_LOW - DOM_LOW)
        frac = max(0, min(1, frac))
        btc = 1 - frac
        alts = frac
        return {"btc": btc, "alts": alts, "stables": 0}

    # HIGH DOM REGION: 0.79 → 0.81+
    if dom > DOM_MID_HIGH:
        frac = (dom - DOM_MID_HIGH) / (DOM_HIGH - DOM_MID_HIGH)
        frac = max(0, min(1, frac))
        btc = 1 - frac
        alts = frac
        return {"btc": btc, "alts": alts, "stables": 0}

    return {"btc": 0, "alts": 0, "stables": 1}


# ---------------- BALANCE STORAGE ----------------

def load_balance():
    if not os.path.exists(BAL_FILE):
        return STARTING_BAL
    df = pd.read_csv(BAL_FILE)
    return float(df.iloc[-1]["balance"])


def save_balance(bal):
    df = pd.DataFrame([{"timestamp": datetime.utcnow(), "balance": bal}])
    if os.path.exists(BAL_FILE):
        old = pd.read_csv(BAL_FILE)
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(BAL_FILE, index=False)


def load_last_alloc():
    if not os.path.exists(ALLOC_FILE):
        return {"btc": 0, "alts": 0, "stables": 1}
    df = pd.read_csv(ALLOC_FILE)
    row = df.iloc[-1]
    return {"btc": row["btc"], "alts": row["alts"], "stables": row["stables"]}


def save_alloc(w):
    df = pd.DataFrame([{
        "timestamp": datetime.utcnow(),
        "btc": w["btc"], "alts": w["alts"], "stables": w["stables"]
    }])
    if os.path.exists(ALLOC_FILE):
        old = pd.read_csv(ALLOC_FILE)
        df = pd.concat([old, df], ignore_index=True)
    df.to_csv(ALLOC_FILE, index=False)


# ---------------- FORMAT MESSAGE ----------------

def format_message():
    balance = load_balance()
    last_alloc = load_last_alloc()

    hmi, hmi_date = load_latest_hmi()
    band, emoji = hmi_band(hmi)

    prices = fetch_prices()
    dom = fetch_btc_dom()

    w = allocation_from_dom_and_hmi(dom, hmi)
    save_alloc(w)

    btc_pct = round(dom * 100, 1)
    alt_pct = round((1 - dom) * 100, 1)

    # Allocation display yesterday→today
    def alloc_line(asset, pct1, pct2):
        val1 = balance * pct1
        val2 = balance * pct2
        return f" • {asset}: {pct1*100:.1f}% → {pct2*100:.1f}% | ${val1:.2f} → ${val2:.2f}"

    alloc_text = "\n".join([
        alloc_line("BTC", last_alloc["btc"], w["btc"]),
        alloc_line("ALTs", last_alloc["alts"], w["alts"]),
        alloc_line("Stables", last_alloc["stables"], w["stables"]),
    ])

    # Action: % of BTC moved
    move = abs(w["btc"] - last_alloc["btc"])
    if last_alloc["btc"] > 0:
        pct_move = round(100 * move / last_alloc["btc"], 1)
    else:
        pct_move = round(move * 100, 1)

    if move < 0.001:
        action = "No reallocation."
    elif w["btc"] < last_alloc["btc"]:
        action = f"Rotate ~{pct_move}% of your BTC → ALTs."
    else:
        action = f"Rotate ~{pct_move}% of your ALTs → BTC."

    # Hive ROI
    roi = round((balance - STARTING_BAL) / STARTING_BAL * 100, 2)

    # BTC B&H ROI
    start_price = prices["BTC"]
    btc_hold_value = STARTING_BAL * (prices["BTC"] / start_price)
    btc_roi = round((btc_hold_value - STARTING_BAL) / STARTING_BAL * 100, 2)

    # Comparison
    if btc_roi == 0:
        mult = 0
    else:
        mult = round(roi / btc_roi, 2)

    # Trades count
    if os.path.exists(ALLOC_FILE):
        df = pd.read_csv(ALLOC_FILE)
        trades = len(df) - 1
    else:
        trades = 0

    return f"""
Hive    {hmi_date}

{emoji} HMI: {hmi:.1f} — {band}
📊 BTC vs EthBnbSol: {btc_pct}/{alt_pct}

💰 Prices:
 • BTC: ${prices['BTC']:.0f}
 • ETH: ${prices['ETH']:.0f}
 • BNB: ${prices['BNB']:.0f}
 • SOL: ${prices['SOL']:.2f}

🧠 Suggested allocation:
{alloc_text}

⚙️ Action:
{action}

🧠 Hive ROI: {roi}%
BTC buy & hold ROI: {btc_roi}%
Hive vs BTC: {mult}x

Hive trades: {trades}
    """.strip()


# ---------------- TELEGRAM ----------------

def send_tg(text):
    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("Telegram env vars missing.")
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"})


# ---------------- MAIN ----------------

def main():
    msg = format_message()
    send_tg(msg)
    print("Sent Hive update.")


if __name__ == "__main__":
    main()
     
