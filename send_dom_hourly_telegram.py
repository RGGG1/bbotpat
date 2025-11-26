#!/usr/bin/env python3
"""
send_dom_hourly_telegram.py

Reads:
- dom_signals_hourly.json
- dom_trade_plan.json

Builds a status message and sends it via Telegram bot API.

Requires:
- TELEGRAM_DOM_BOT_TOKEN
- TELEGRAM_DOM_CHAT_ID

If these are not set, it will just print the message to stdout.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

import urllib.parse
import urllib.request

ROOT = Path(".")
SIGNALS_FILE = ROOT / "dom_signals_hourly.json"
PLAN_FILE = ROOT / "dom_trade_plan.json"


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def fmt_pct(frac: Any) -> str:
    try:
        x = float(frac)
    except Exception:
        return "--.--%"
    pct = x * 100.0
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.2f}%"


def fmt_usd(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        return "--"
    return f"${v:.2f}"


def build_message(sig: Dict[str, Any], plan: Dict[str, Any]) -> str:
    pos = sig.get("position", {}) or {}
    hmi = sig.get("hmi", None)
    hmi_band = sig.get("hmi_band", "")

    equity = sig.get("equity_usd", None)
    roi_frac = sig.get("roi_frac", None)

    pos_type = (pos.get("type") or "").upper()
    token = (pos.get("token") or "NONE").upper()
    entry_price = pos.get("entry_price", None)
    cur_price = pos.get("current_price", None)
    target_price = pos.get("target_price", None)
    hmi_override = bool(pos.get("hmi_override", False))

    plan_action = plan.get("plan_action", "HOLD")
    target_type = plan.get("target_type", "")
    target_token = plan.get("target_token", "")
    notes = plan.get("notes", "")

    lines = []
    lines.append("[DOM Model]")
    lines.append(f"Equity: {fmt_usd(equity)} (ROI: {fmt_pct(roi_frac)})")

    if hmi is not None:
        lines.append(f"HMI: {hmi:.1f} ({hmi_band})")

    if hmi_override:
        lines.append("HMI override: ACTIVE")
    else:
        lines.append("HMI override: inactive")

    if pos_type == "STABLES":
        lines.append("Holding: STABLES (USDC)")
    else:
        lines.append(f"Holding: {pos_type} {token}")

    if entry_price is not None:
        lines.append(f"Entry: {fmt_usd(entry_price)}")
    if cur_price is not None:
        lines.append(f"Current: {fmt_usd(cur_price)}")
    if target_price is not None:
        lines.append(f"Target: {fmt_usd(target_price)}")

    lines.append(f"Plan action: {plan_action}")
    if target_type and target_token:
        lines.append(f"Plan target: {target_type} {target_token}")
    if notes:
        lines.append(f"Notes: {notes}")

    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    token = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")



    if not token or not chat_id:
        print("[send_dom_hourly_telegram] TELEGRAM_DOM_BOT_TOKEN or TELEGRAM_DOM_CHAT_ID not set.")
        print("[send_dom_hourly_telegram] Message would have been:\n")
        print(text)
        return

    base_url = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    url = base_url + "?" + urllib.parse.urlencode(params)

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        print(f"[send_dom_hourly_telegram] Sent message, response: {body[:200]}")
    except Exception as e:
        print(f"[send_dom_hourly_telegram] ERROR sending message: {e}")
        print("[send_dom_hourly_telegram] Message was:\n")
        print(text)


def main() -> None:
    if not SIGNALS_FILE.exists():
        print("[send_dom_hourly_telegram] dom_signals_hourly.json not found; aborting.")
        return
    if not PLAN_FILE.exists():
        print("[send_dom_hourly_telegram] dom_trade_plan.json not found; aborting.")
        return

    sig = load_json(SIGNALS_FILE)
    plan = load_json(PLAN_FILE)

    text = build_message(sig, plan)
    send_telegram_message(text)


if __name__ == "__main__":
    main()
