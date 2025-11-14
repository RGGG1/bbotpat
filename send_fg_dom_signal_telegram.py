#!/usr/bin/env python3
"""
Send Telegram alert with:

- Current FG_lite % and band name (zombie, extreme fear, etc)
- Emoji colour based on fear/greed
- BTC dominance vs ALT (BTC:ALT %)
- BTC / ETH / SOL prices
- Current trade signal from dominance + FG_lite model
"""

import os
import time
from datetime import datetime

import requests
import pandas as pd
import numpy as np

COINBASE_BASE   = "https://api.exchange.coinbase.com"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"

DOM_LOW   = 0.75
DOM_HIGH  = 0.81
DOM_MID_LOW  = 0.771
DOM_MID_HIGH = 0.789
GREED_STABLE_THRESHOLD = 77.0

RISK_IDS = ["bitcoin", "ethereum", "solana", "binancecoin"]

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")


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


def load_latest_fg(path="output/fg2_daily.csv"):
    df = pd.read_csv(path, parse_dates=["date"])
    df = df.sort_values("date")
    row = df.iloc[-1]
    return float(row["FG_lite"]), row["date"].date()


def fg_band_name_and_emoji(fg):
    # Your bands:
    # <10 zombie apocalypse
    # 10-25 extreme fear
    # 25-35 moderate fear
    # 35-65 neutral
    # 65-75 greed
    # 75-90 extreme greed
    # 90+ the top
    if fg < 10:
        return "zombie apocalypse", "🩸🧟‍♂️"
    elif fg < 25:
        return "extreme fear", "🟥😱"
    elif fg < 35:
        return "moderate fear", "🟧😟"
    elif fg < 65:
        return "neutral", "⬜😐"
    elif fg < 75:
        return "greed", "🟩😏"
    elif fg < 90:
        return "extreme greed", "🟩🟩🤪"
    else:
        return "the top", "🟢🚨"

def allocation_from_dom_and_fg(btc_dom, fg_lite):
    # 1) mid dominance -> stables
    if DOM_MID_LOW <= btc_dom <= DOM_MID_HIGH:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 2) extreme greed -> stables
    if fg_lite >= GREED_STABLE_THRESHOLD:
        return {"btc": 0.0, "alts": 0.0, "stables": 1.0}

    # 3) dominance rotation
    if btc_dom <= DOM_LOW:
        btc_w, alt_w = 1.0, 0.0
    elif btc_dom >= DOM_HIGH:
        btc_w, alt_w = 0.0, 1.0
    else:
        t = (btc_dom - DOM_LOW) / (DOM_HIGH - DOM_LOW)
        btc_w = 1.0 - t
        alt_w = t

    return {"btc": btc_w, "alts": alt_w, "stables": 0.0}


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


def format_signal_message():
    fg, fg_date = load_latest_fg()
    band_name, emoji = fg_band_name_and_emoji(fg)

    prices = fetch_spot_prices()
    btc_dom = fetch_btc_dom_current()
    w = allocation_from_dom_and_fg(btc_dom, fg)

    btc_pct  = round(btc_dom * 100, 1)
    alt_pct  = round((1 - btc_dom) * 100, 1)

    # describe trade signal qualitatively
    if w["stables"] == 1.0:
        signal = "Stable all (100% stables)"
    elif w["btc"] > w["alts"]:
        signal = f"Rotate toward BTC ({int(w['btc']*100)}% BTC / {int(w['alts']*100)}% ALTs)"
    else:
        signal = f"Rotate toward ALTs ({int(w['alts']*100)}% ALTs / {int(w['btc']*100)}% BTC)"

    text = (
        f"🧠 *FG_lite & Dominance Signal*\n"
        f"`Date:` {fg_date}\n\n"
        f"{emoji} *FG_lite:* {fg:.1f} — _{band_name}_\n"
        f"📊 *BTC Dominance:* {btc_pct:.1f}% (ALTs {alt_pct:.1f}%)\n\n"
        f"💰 *Prices:*\n"
        f" • BTC: ${prices['BTC']:.0f}\n"
        f" • ETH: ${prices['ETH']:.0f}\n"
        f" • SOL: ${prices['SOL']:.2f}\n\n"
        f"⚙️ *Signal:* {signal}\n"
        f"(Greed stabling threshold: FG ≥ {GREED_STABLE_THRESHOLD})"
    )
    return text


def send_telegram_message(text: str):
    if not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("Telegram env vars not set (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
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
  
