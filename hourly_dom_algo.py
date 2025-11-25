#!/usr/bin/env python3
"""
hourly_dom_algo.py

Hourly dominance-based strategy (DOM model) - SIMULATION ONLY for now.

- Reads:
    * hmi_latest.json (root or docs/)
    * prices_latest.json (root or docs/)
    * dom_hourly_state.json (state, root only; created if missing)
- Uses per-token BTC dominance (btc_dom) and "range" string from prices_latest.json
  to derive dominance ranges and positions.
- Decides what we *should* hold:
    * STABLES (USDC)
    * BTC
    * One ALT from the dominance universe (strongest signal, with mcap tiebreak)
- Applies rules:
    * HMI override (risk-off) -> flatten to STABLES
    * ALT vs BTC regime at 50/50 cutoff
    * Exit ALT when its own dominance mean-reverts into neutral band
    * Rotate from current ALT to another ALT if its score is >5 points higher
    * All moves are full equity (100% of current value) into new asset
- Writes:
    * dom_hourly_state.json (root)
    * dom_signals_hourly.json (root + docs) for website & Telegram
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(".")
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True, parents=True)

STATE_FILE = ROOT / "dom_hourly_state.json"
SIGNALS_ROOT = ROOT / "dom_signals_hourly.json"
SIGNALS_DOCS = DOCS / "dom_signals_hourly.json"

INITIAL_EQUITY_USD = 100.0

# HMI override threshold: risk-off if HMI < 45 ("NGMI" or worse)
HMI_RISK_OFF_THRESHOLD = 45.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json_first(paths: List[Path]) -> Optional[Dict[str, Any]]:
    for p in paths:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


def load_hmi() -> Tuple[Optional[float], str]:
    js = load_json_first([ROOT / "hmi_latest.json", DOCS / "hmi_latest.json"])
    if not js:
        return None, ""
    try:
        hmi = float(js.get("hmi"))
    except Exception:
        hmi = None
    band = js.get("band", "") or ""
    return hmi, band


def load_prices() -> Optional[Dict[str, Any]]:
    return load_json_first([ROOT / "prices_latest.json", DOCS / "prices_latest.json"])


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    # Initial state: all in stables
    now = now_iso()
    return {
        "equity_usd": INITIAL_EQUITY_USD,
        "position_type": "STABLES",      # "STABLES" | "BTC" | "ALT"
        "position_token": "NONE",        # "BTC", "SOL", etc, or "NONE"
        "position_units": INITIAL_EQUITY_USD,  # 1 unit = 1 USDC
        "entry_price": 1.0,
        "entry_timestamp": now,
        "last_update": now,
        "base_timestamp": now,
        "base_balance_usd": INITIAL_EQUITY_USD,
        "mode": "SIM",
    }


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def parse_range(range_str: str) -> Optional[Tuple[float, float]]:
    """
    Parse a range like "73–90%" or "73-90%" into (73.0, 90.0).
    """
    if not range_str:
        return None
    import re

    m = re.search(r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)", str(range_str))
    if not m:
        return None
    try:
        low = float(m.group(1))
        high = float(m.group(2))
    except Exception:
        return None
    if high <= low:
        return None
    return low, high


def build_price_maps(prices_js: Dict[str, Any]) -> Tuple[Dict[str, float], Dict[str, float]]:
    price_map: Dict[str, float] = {}
    mc_map: Dict[str, float] = {}
    rows = prices_js.get("rows", []) or []
    for row in rows:
        token = str(row.get("token", "")).upper()
        if not token:
            continue
        try:
            price = float(row.get("price", 0.0))
        except Exception:
            price = 0.0
        try:
            mc = float(row.get("mc", 0.0))
        except Exception:
            mc = 0.0
        price_map[token] = price
        mc_map[token] = mc
    return price_map, mc_map


def extract_alt_dominance(prices_js: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build per-alt dominance info from prices_latest.json rows:
        - token
        - dom_current (btc_dom)
        - dom_low, dom_high (from "range" column)
    Skip BTC and stables.
    """
    alts: List[Dict[str, Any]] = []
    rows = prices_js.get("rows", []) or []
    for row in rows:
        token = str(row.get("token", "")).upper()
        if not token or token == "BTC":
            continue
        # crude stable detection: USD in symbol
        if "USD" in token:
            continue
        btc_dom = row.get("btc_dom", None)
        if btc_dom is None:
            continue
        try:
            dom_cur = float(btc_dom)
        except Exception:
            continue
        rng_str = row.get("range", "") or ""
        rng = parse_range(rng_str)
        if not rng:
            continue
        dom_low, dom_high = rng
        alts.append(
            {
                "token": token,
                "dom_current": dom_cur,
                "dom_low": dom_low,
                "dom_high": dom_high,
            }
        )
    return alts


def compute_alt_score(dom_low: float, dom_high: float, dom_current: float) -> float:
    """
    Map dominance position to 0-100 score:
        0   -> BTC dominance at low end (ALT expensive vs BTC)
        100 -> BTC dominance at high end (ALT cheap vs BTC)
    """
    if dom_high <= dom_low:
        return 0.0
    z = (dom_current - dom_low) / (dom_high - dom_low)
    return max(0.0, min(100.0, z * 100.0))


def pick_best_alt(alts: List[Dict[str, Any]], mc_map: Dict[str, float]) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Pick best ALT by score, breaking ties by higher market cap.
    Returns (alt_dict_or_None, best_score).
    """
    best: Optional[Dict[str, Any]] = None
    best_score = -1.0
    best_mc = -1.0

    for alt in alts:
        token = alt["token"]
        dom_low = float(alt["dom_low"])
        dom_high = float(alt["dom_high"])
        dom_cur = float(alt["dom_current"])

        score = compute_alt_score(dom_low, dom_high, dom_cur)
        alt["score"] = score

        mc = float(mc_map.get(token, 0.0))

        if score > best_score + 1e-6:
            best = alt
            best_score = score
            best_mc = mc
        elif abs(score - best_score) <= 1e-6 and mc > best_mc:
            best = alt
            best_score = score
            best_mc = mc

    return best, best_score


def compute_neutral_band(dom_low: float, dom_high: float) -> Tuple[float, float]:
    """
    Middle 20% of the range: [40%, 60%] into the band.
    """
    width = dom_high - dom_low
    return dom_low + 0.40 * width, dom_low + 0.60 * width


def compute_alt_target_price(
    token: str,
    neutral_high: float,
    price_map: Dict[str, float],
    mc_map: Dict[str, float],
) -> Optional[float]:
    """
    Given a target dominance (neutral_high as %), BTC mc, and current alt supply,
    compute implied target alt price.
    """
    token = token.upper()
    btc_mc = float(mc_map.get("BTC", 0.0))
    alt_mc_now = float(mc_map.get(token, 0.0))
    price_now = float(price_map.get(token, 0.0))

    if btc_mc <= 0 or alt_mc_now <= 0 or price_now <= 0:
        return None

    dom_target = neutral_high / 100.0
    if dom_target <= 0 or dom_target >= 1:
        return None

    # dom = B / (B + S) => S = (1 - dom) / dom * B
    s_target = (1.0 - dom_target) / dom_target * btc_mc

    supply_est = alt_mc_now / price_now
    if supply_est <= 0:
        return None

    return s_target / supply_est


def main() -> None:
    now = now_iso()

    # 1) Load inputs
    hmi, hmi_band = load_hmi()
    prices_js = load_prices()
    if not prices_js:
        print("[dom_hourly] prices_latest.json not found or invalid; aborting.")
        return

    price_map, mc_map = build_price_maps(prices_js)
    alts = extract_alt_dominance(prices_js)

    # 2) Load state
    state = load_state()
    pos_type = str(state.get("position_type", "STABLES")).upper()
    pos_token = str(state.get("position_token", "NONE")).upper()
    pos_units = float(state.get("position_units", 0.0))
    entry_price = float(state.get("entry_price", 1.0))
    base_balance = float(state.get("base_balance_usd", INITIAL_EQUITY_USD))

    # 3) Determine current price of position and update equity
    current_price = 1.0
    if pos_type == "STABLES":
        current_price = 1.0
    elif pos_type == "BTC":
        current_price = float(price_map.get("BTC", 0.0)) or 0.0
    elif pos_type == "ALT":
        current_price = float(price_map.get(pos_token, 0.0)) or 0.0

    equity = pos_units * current_price if current_price > 0 else state.get("equity_usd", INITIAL_EQUITY_USD)

    # 4) HMI override?
    hmi_override = False
    if hmi is not None and hmi < HMI_RISK_OFF_THRESHOLD:
        hmi_override = True

    # 5) Compute best ALT & regime if no override
    best_alt: Optional[Dict[str, Any]] = None
    best_score = -1.0
    regime = "BTC"  # default

    if not hmi_override and alts:
        best_alt, best_score = pick_best_alt(alts, mc_map)
        if best_alt is not None and best_score > 50.0:
            regime = "ALT"
        else:
            regime = "BTC"

    # 6) Decide action: HOLD / SWITCH / FLATTEN_TO_STABLES
    action = "HOLD"
    from_token = pos_token
    to_token = pos_token

    # HMI override always flattens to stables
    if hmi_override:
        if pos_type != "STABLES":
            action = "FLATTEN_TO_STABLES"
            to_token = "NONE"

    else:
        # No HMI override
        if pos_type == "STABLES":
            if regime == "ALT" and best_alt is not None:
                action = "SWITCH"
                from_token = "NONE"
                to_token = best_alt["token"]
            else:
                action = "SWITCH"
                from_token = "NONE"
                to_token = "BTC"

        elif pos_type == "BTC":
            if regime == "ALT" and best_alt is not None:
                action = "SWITCH"
                from_token = "BTC"
                to_token = best_alt["token"]
            else:
                action = "HOLD"
                to_token = "BTC"

        elif pos_type == "ALT":
            # Check mean-reversion exit for this ALT
            alt_row = next((a for a in alts if a["token"] == pos_token), None)
            if alt_row is None:
                # If we lost dominance data for this token, be safe and flatten
                action = "FLATTEN_TO_STABLES"
                to_token = "NONE"
            else:
                L = float(alt_row["dom_low"])
                H = float(alt_row["dom_high"])
                C = float(alt_row["dom_current"])
                neutral_low, neutral_high = compute_neutral_band(L, H)

                # Exit if dominance has mean-reverted into neutral
                if C <= neutral_high:
                    action = "FLATTEN_TO_STABLES"
                    to_token = "NONE"
                else:
                    # Consider rotation to a stronger ALT if available
                    if best_alt is not None:
                        score_current = float(alt_row.get("score", compute_alt_score(L, H, C)))
                        score_best = float(best_alt.get("score", 0.0))
                        if (
                            best_alt["token"] != pos_token
                            and score_best > 50.0
                            and (score_best - score_current) > 5.0
                        ):
                            action = "SWITCH"
                            from_token = pos_token
                            to_token = best_alt["token"]
                        else:
                            action = "HOLD"
                            to_token = pos_token
                    else:
                        action = "HOLD"
                        to_token = pos_token

    # 7) Apply action to state (SIM executor)
    # Equity already updated from previous position; we always move full equity
    new_pos_type = pos_type
    new_pos_token = pos_token
    new_pos_units = pos_units
    new_entry_price = entry_price

    if action == "FLATTEN_TO_STABLES":
        new_pos_type = "STABLES"
        new_pos_token = "NONE"
        new_pos_units = equity  # 1 USDC per unit
        new_entry_price = 1.0

    elif action == "SWITCH":
        # Compute target token type
        tgt = to_token.upper()
        if tgt == "BTC":
            new_pos_type = "BTC"
        elif tgt == "NONE":
            new_pos_type = "STABLES"
        else:
            new_pos_type = "ALT"

        # price for new token
        if new_pos_type == "STABLES":
            tgt_price = 1.0
        elif new_pos_type == "BTC":
            tgt_price = float(price_map.get("BTC", 0.0)) or 0.0
        else:
            tgt_price = float(price_map.get(tgt, 0.0)) or 0.0

        if tgt_price <= 0:
            # if we cannot price the new token, do nothing (hold old position)
            new_pos_type = pos_type
            new_pos_token = pos_token
            new_pos_units = pos_units
            new_entry_price = entry_price
        else:
            new_pos_token = tgt
            new_pos_units = equity / tgt_price
            new_entry_price = tgt_price

    # Update state
    state["equity_usd"] = float(equity)
    state["position_type"] = new_pos_type
    state["position_token"] = new_pos_token
    state["position_units"] = float(new_pos_units)
    state["entry_price"] = float(new_entry_price)
    state["entry_timestamp"] = now if action in ("FLATTEN_TO_STABLES", "SWITCH") else state.get("entry_timestamp")
    state["last_update"] = now
    state["base_balance_usd"] = base_balance

    save_state(state)

    # 8) Build signals JSON for website
    # Recompute current price of *new* position
    if new_pos_type == "STABLES":
        cur_price = 1.0
    elif new_pos_type == "BTC":
        cur_price = float(price_map.get("BTC", 0.0)) or 0.0
    else:
        cur_price = float(price_map.get(new_pos_token, 0.0)) or 0.0

    # Compute overall ROI vs initial $100
    roi_frac: Optional[float]
    try:
        if base_balance > 0:
            roi_frac = (equity / base_balance) - 1.0
        else:
            roi_frac = None
    except Exception:
        roi_frac = None

    # Dynamic target price (for ALT positions and BTC context)
    target_price: Optional[float] = None

    if new_pos_type == "ALT":
        alt_info = next((a for a in alts if a["token"] == new_pos_token), None)
        if alt_info is not None:
            L = float(alt_info["dom_low"])
            H = float(alt_info["dom_high"])
            _, neutral_high = compute_neutral_band(L, H)
            target_price = compute_alt_target_price(new_pos_token, neutral_high, price_map, mc_map)
    elif new_pos_type == "BTC":
        # derive target based on best ALT's neutral band (when we might rotate out)
        if best_alt is not None:
            L = float(best_alt["dom_low"])
            H = float(best_alt["dom_high"])
            _, neutral_high = compute_neutral_band(L, H)
            # We don't compute an explicit BTC price target here; leave None for now.
            target_price = None
    else:
        target_price = None

    position_payload = {
        "type": new_pos_type,
        "token": new_pos_token,
        "entry_price": float(new_entry_price),
        "current_price": float(cur_price),
        "target_price": float(target_price) if target_price is not None else None,
        "hmi_override": bool(hmi_override),
    }

    signals_payload = {
        "timestamp": now,
        "equity_usd": float(equity),
        "roi_frac": roi_frac,
        "position": position_payload,
        "hmi": hmi,
        "hmi_band": hmi_band,
        "action": action,
    }

    txt = json.dumps(signals_payload, indent=2)
    SIGNALS_ROOT.write_text(txt)
    SIGNALS_DOCS.write_text(txt)

    print(f"[dom_hourly] Updated state + signals at {now} (action={action}, pos={new_pos_type}/{new_pos_token})")


if __name__ == "__main__":
    main()
