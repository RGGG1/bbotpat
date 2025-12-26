#!/usr/bin/env python3
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List


WEBROOT = Path("/var/www/bbotpat_live")
HMI_PATH = WEBROOT / "hmi_latest.json"
PRICES_PATH = WEBROOT / "prices_latest.json"

KC3_OUT = WEBROOT / "kc3_latest.json"
KC3_CSV = WEBROOT / "kc3_history.csv"

# Paper settings
START_EQUITY_USD = 100.0
HMI_MOMENTUM_THRESHOLD = 0.1  # per your spec


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def parse_range(range_str: str) -> Optional[Tuple[float, float]]:
    """
    Parses strings like '73.1–90.1%' or '73.10% – 90.10%'.
    Returns (low, high) floats in percent.
    """
    if not range_str:
        return None
    s = str(range_str)
    # normalize dash variants
    s = s.replace("–", "-").replace("—", "-")
    # remove percent signs
    s = s.replace("%", "")
    # split on dash
    parts = [p.strip() for p in s.split("-")]
    if len(parts) < 2:
        return None
    lo = safe_float(parts[0])
    hi = safe_float(parts[1])
    if lo is None or hi is None or hi <= lo:
        return None
    return lo, hi


def compute_potential_roi_frac(row: Dict[str, Any], btc_mc: float) -> Optional[float]:
    """
    Mirrors the existing frontend/backend "neutralHigh target" idea:
      neutralHigh = low + 0.60*(high-low)
      domTarget = neutralHigh / 100
      S_target = (1-dom)/dom * btc_mc
      supply_est = alt_mc / price_now
      targetPrice = S_target / supply_est
      roi = targetPrice/price_now - 1
    Requires: row['mc'], row['price'], row['range'] and btc_mc
    """
    token = (row.get("token") or "").upper()
    if not token or token == "BTC" or "USD" in token:
        return None

    rng = parse_range(row.get("range") or "")
    if not rng:
        return None
    low, high = rng
    neutral_high = low + 0.60 * (high - low)
    dom_target = neutral_high / 100.0
    if dom_target <= 0 or dom_target >= 1:
        return None

    alt_mc = safe_float(row.get("mc"))
    price_now = safe_float(row.get("price"))
    if not alt_mc or not price_now or alt_mc <= 0 or price_now <= 0:
        return None
    if btc_mc <= 0:
        return None

    s_target = (1.0 - dom_target) / dom_target * btc_mc

    supply_est = alt_mc / price_now
    if supply_est <= 0:
        return None

    target_price = s_target / supply_est
    if target_price <= 0:
        return None

    return (target_price / price_now) - 1.0


def pick_best_token(rows: List[Dict[str, Any]]) -> Optional[Tuple[str, float]]:
    """
    Returns (token, pot_roi_frac) for the highest pot ROI token.
    """
    btc_row = next((r for r in rows if (r.get("token") or "").upper() == "BTC"), None)
    btc_mc = safe_float(btc_row.get("mc")) if btc_row else None
    if not btc_mc:
        return None

    best_tok = None
    best_roi = None
    for r in rows:
        roi = compute_potential_roi_frac(r, btc_mc)
        if roi is None:
            continue
        if best_roi is None or roi > best_roi:
            best_roi = roi
            best_tok = (r.get("token") or "").upper()

    if best_tok is None or best_roi is None:
        return None
    return best_tok, best_roi


def get_price(rows: List[Dict[str, Any]], token: str) -> Optional[float]:
    t = token.upper()
    r = next((x for x in rows if (x.get("token") or "").upper() == t), None)
    if not r:
        return None
    return safe_float(r.get("price"))


@dataclass
class Position:
    side: str  # "LONG" or "SHORT"
    token: str
    entry_price: float
    entry_time: str


class KC3Paper:
    def __init__(self):
        self.equity = START_EQUITY_USD
        self.position: Optional[Position] = None
        self.prev_hmi: Optional[float] = None

        # ensure history file exists with headers
        if not KC3_CSV.exists():
            with KC3_CSV.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "ts", "hmi", "hmi_delta", "signal_side",
                    "best_token", "best_pot_roi_pct",
                    "position_side", "position_token", "entry_price",
                    "mark_price", "equity_usd", "position_pnl_usd"
                ])

    def mark_to_market(self, mark_price: float) -> float:
        """
        Returns unrealized PnL in USD on the current position, assuming full equity sized at entry.
        For paper simplicity, we treat notional = equity_at_entry (100% allocation).
        """
        if not self.position:
            return 0.0
        e = self.position.entry_price
        if e <= 0 or mark_price <= 0:
            return 0.0
        if self.position.side == "LONG":
            return self.equity * (mark_price / e - 1.0)
        else:
            return self.equity * (e / mark_price - 1.0)

    def close_and_realize(self, mark_price: float):
        if not self.position:
            return
        pnl = self.mark_to_market(mark_price)
        self.equity = max(0.0, self.equity + pnl)
        self.position = None

    def open_position(self, side: str, token: str, price: float):
        self.position = Position(
            side=side,
            token=token,
            entry_price=price,
            entry_time=utc_now_iso()
        )

    def write_latest(self, payload: Dict[str, Any]):
        KC3_OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def append_history(self, row: List[Any]):
        with KC3_CSV.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

    def step(self):
        # load inputs
        if not PRICES_PATH.exists() or not HMI_PATH.exists():
            return

        prices = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
        rows = prices.get("rows", [])
        hmi_js = json.loads(HMI_PATH.read_text(encoding="utf-8"))
        hmi = safe_float(hmi_js.get("hmi"))
        if hmi is None or not rows:
            return

        best = pick_best_token(rows)
        if not best:
            return
        best_token, best_pot_roi = best

        # compute momentum
        hmi_delta = None
        if self.prev_hmi is not None:
            hmi_delta = hmi - self.prev_hmi

        # per spec: LONG only if delta >= 0.99, else SHORT
        signal_side = "SHORT"
        if hmi_delta is not None and hmi_delta >= HMI_MOMENTUM_THRESHOLD:
            signal_side = "LONG"

        # get mark/entry prices
        best_price = get_price(rows, best_token)
        if best_price is None or best_price <= 0:
            return

        # handle switching logic
        if self.position is None:
            self.open_position(signal_side, best_token, best_price)
        else:
            # decide if we need to change position due to side or token change
            need_switch = (self.position.side != signal_side) or (self.position.token != best_token)
            if need_switch:
                # close at current price of current position token
                cur_mark = get_price(rows, self.position.token) or best_price
                self.close_and_realize(cur_mark)
                # open new at best_token price
                self.open_position(signal_side, best_token, best_price)

        # mark-to-market (for reporting)
        pos_pnl = 0.0
        mark_price = None
        if self.position:
            mark_price = get_price(rows, self.position.token) or self.position.entry_price
            pos_pnl = self.mark_to_market(mark_price)

        # output payload
        out = {
            "timestamp": utc_now_iso(),
            "hmi": round(hmi, 1),
            "hmi_delta": None if hmi_delta is None else round(hmi_delta, 3),
            "signal_side": signal_side,
            "best_token": best_token,
            "best_pot_roi_pct": round(best_pot_roi * 100.0, 2),
            "equity_usd": round(self.equity, 4),
            "position": None if not self.position else {
                "side": self.position.side,
                "token": self.position.token,
                "entry_price": round(self.position.entry_price, 8),
                "entry_time": self.position.entry_time,
                "mark_price": None if mark_price is None else round(mark_price, 8),
                "unrealized_pnl_usd": round(pos_pnl, 6),
            },
        }
        self.write_latest(out)

        self.append_history([
            out["timestamp"],
            out["hmi"],
            out["hmi_delta"],
            out["signal_side"],
            out["best_token"],
            out["best_pot_roi_pct"],
            (self.position.side if self.position else ""),
            (self.position.token if self.position else ""),
            (round(self.position.entry_price, 8) if self.position else ""),
            (round(mark_price, 8) if mark_price is not None else ""),
            out["equity_usd"],
            (round(pos_pnl, 6) if self.position else 0.0),
        ])

        self.prev_hmi = hmi


def main():
    agent = KC3Paper()
    while True:
        try:
            agent.step()
        except Exception:
            # keep it resilient
            pass
        time.sleep(1)  # every second


if __name__ == "__main__":
    main()
