#!/usr/bin/env python3
"""
update_dom_mc_history.py

Reads docs/prices_latest.json, extracts market caps for your core token universe,
and appends today's snapshot into docs/dom_mc_history.json so that the API can
compute arbitrary DOM combos historically.

Run this AFTER send_fg_dom_signal_telegram.py (i.e. after prices_latest.json is updated).
"""

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"

PRICES_DOCS = DOCS / "prices_latest.json"
PRICES_ROOT = ROOT / "prices_latest.json"

DOM_MC_HISTORY_DOCS = DOCS / "dom_mc_history.json"
DOM_MC_HISTORY_ROOT = ROOT / "dom_mc_history.json"

# Same universe as your website DOM widget
UNIVERSE_TOKENS = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]

MAX_DAYS = 730  # keep about 2 years


def load_prices():
    path = PRICES_DOCS if PRICES_DOCS.exists() else PRICES_ROOT
    if not path.exists():
        raise SystemExit(f"prices_latest.json not found at {PRICES_DOCS} or {PRICES_ROOT}")
    data = json.loads(path.read_text())
    rows = data.get("rows", [])
    # Build token -> mc mapping
    mc_map = {}
    for row in rows:
        token = row.get("token")
        if token not in UNIVERSE_TOKENS:
            continue
        mc_val = row.get("mc")
        try:
            mc = float(mc_val)
        except Exception:
            mc = 0.0
        mc_map[token] = mc
    ts = data.get("timestamp")
    return ts, mc_map


def load_history():
    """
    Returns dict with structure:
    {
      "series": [
        { "date": "YYYY-MM-DD", "mc": { "BTC": ..., "ETH": ..., ... } },
        ...
      ]
    }
    """
    if DOM_MC_HISTORY_DOCS.exists():
        path = DOM_MC_HISTORY_DOCS
    elif DOM_MC_HISTORY_ROOT.exists():
        path = DOM_MC_HISTORY_ROOT
    else:
        return {"series": []}

    try:
        return json.loads(path.read_text())
    except Exception:
        # If file is corrupted, start fresh rather than crash the DOM job
        return {"series": []}


def save_history(history):
    text = json.dumps(history, indent=2, sort_keys=False)
    DOM_MC_HISTORY_ROOT.write_text(text)
    DOM_MC_HISTORY_DOCS.write_text(text)


def main():
    ts, mc_map = load_prices()
    today = datetime.utcnow().date().isoformat()

    history = load_history()
    series = history.get("series", [])
    if not isinstance(series, list):
        series = []

    # If last entry is today, overwrite it; else append
    if series and series[-1].get("date") == today:
        series[-1]["mc"] = mc_map
    else:
        series.append({
            "date": today,
            "mc": mc_map,
        })

    # Trim to last MAX_DAYS entries
    if len(series) > MAX_DAYS:
        series = series[-MAX_DAYS:]

    history["series"] = series
    history["tokens"] = UNIVERSE_TOKENS
    history["last_updated"] = ts or datetime.utcnow().isoformat() + "Z"

    save_history(history)


if __name__ == "__main__":
    main()
