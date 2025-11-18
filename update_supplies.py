#!/usr/bin/env python3
"""
update_supplies.py

Weekly job (Sunday 00:00 UTC):

- Fetch circulating supplies for core tokens from CoinGecko.
- Save to supplies_latest.json at repo root and docs/supplies_latest.json.
- Send Telegram alert on failure.

This lets the rest of the system use:
    market cap = Binance price * cached circulating_supply
for market-cap-based dominance, while relying very lightly on CoinGecko.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(".")
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True, parents=True)

SUPPLIES_ROOT = ROOT / "supplies_latest.json"
SUPPLIES_DOCS = DOCS / "supplies_latest.json"

COINGECKO = "https://api.coingecko.com/api/v3"

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

# Symbol -> CoinGecko id
TOKENS_CG = {
    "BTC":  "bitcoin",
    "ETH":  "ethereum",
    "BNB":  "binancecoin",
    "SOL":  "solana",
    "DOGE": "dogecoin",
    "TON":  "the-open-network",
    "SUI":  "sui",
    "UNI":  "uniswap",
    "USDT": "tether",
    "USDC": "usd-coin",
}


def tg_send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print("[supplies] TG token/chat missing; skipping Telegram.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TG_CHAT,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print("[supplies] TG error:", r.text[:200])
    except Exception as e:
        print("[supplies] TG exception:", e)


def cg_get(path, params=None, timeout=60):
    if params is None:
        params = {}
    url = COINGECKO + path
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:200]}")
    return r.json()


def main():
    ids_str = ",".join(TOKENS_CG.values())
    try:
        js = cg_get(
            "/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids_str,
                "order": "market_cap_desc",
                "per_page": 250,
                "page": 1,
                "sparkline": "false",
            },
        )
    except Exception as e:
        msg = f"<b>HiveAI Supplies Update FAILED</b>\n\nError: {e}"
        print("[supplies]", msg)
        tg_send(msg)
        raise SystemExit(1)

    by_id = {row["id"]: row for row in js}

    supplies = {}
    missing = []

    for sym, cg_id in TOKENS_CG.items():
        row = by_id.get(cg_id)
        if not row:
            missing.append(sym)
            continue
        circ = row.get("circulating_supply")
        if circ is None:
            missing.append(sym)
            continue
        supplies[sym] = {
            "id": cg_id,
            "circulating_supply": float(circ),
        }

    payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": "coingecko",
        "supplies": supplies,
        "missing": missing,
    }

    text = json.dumps(payload, indent=2)
    SUPPLIES_ROOT.write_text(text)
    SUPPLIES_DOCS.write_text(text)

    msg_lines = [
        "<b>HiveAI Supplies Update</b>",
        "",
        f"Updated supplies for: {', '.join(sorted(supplies.keys())) or 'none'}",
    ]
    if missing:
        msg_lines.append(f"Missing: {', '.join(missing)}")
    tg_send("\n".join(msg_lines))

    print("[supplies] Wrote supplies_latest.json with",
          len(supplies), "tokens; missing:", missing)


if __name__ == "__main__":
    main()
