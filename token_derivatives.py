from __future__ import annotations

import re
from typing import Optional, Tuple


_NUM_PAIR_RE = re.compile(r"(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)")


def _parse_range_low_high(range_str: str) -> Optional[Tuple[float, float]]:
    """
    Accepts strings like:
      "73.1–90.1%"
      "92.7–96.0%"
      "73.1-90.1%"
    Returns (low, high) floats if valid.
    """
    if not range_str:
        return None
    m = _NUM_PAIR_RE.search(str(range_str))
    if not m:
        return None
    low = float(m.group(1))
    high = float(m.group(2))
    if not (high > low):
        return None
    return low, high


def compute_action_for_row(token: str, btc_dom_pct: Optional[float], range_str: str) -> str:
    """
    Ported from your v1 index.html computeActionForRow(row)
    Returns:
      "–" for BTC or stables/unknown
      "Stables" if within middle band
      or "ALT/BTC" percentages like "70/30"
    """
    if not token:
        return "–"
    up = str(token).upper()

    # No action for BTC or stables
    if up == "BTC" or "USD" in up:
        return "–"

    if btc_dom_pct is None:
        return "–"
    try:
        dom_raw = float(btc_dom_pct)
    except Exception:
        return "–"

    parsed = _parse_range_low_high(range_str or "")
    if not parsed:
        return "–"
    min_pct, max_pct = parsed
    if not (max_pct > min_pct):
        return "–"

    pos = (dom_raw - min_pct) / (max_pct - min_pct)
    if pos != pos:  # NaN
        return "–"
    pos = max(0.0, min(1.0, pos))

    # 40/20/40 bands:
    # 0.0–0.4  : BTC-heavy zone
    # 0.4–0.6  : Stables
    # 0.6–1.0  : ALT-heavy zone
    if 0.4 <= pos <= 0.6:
        return "Stables"

    if pos < 0.4:
        # BTC zone: 100/0 at pos=0 -> 50/50 at pos=0.4
        t = pos / 0.4  # 0..1
        btc_pct = 100 - round(t * 50)  # 100 -> 50
        alt_pct = 100 - btc_pct        # 0   -> 50
    else:
        # ALT zone: 50/50 at pos=0.6 -> 100/0 at pos=1.0
        t = (pos - 0.6) / 0.4  # 0..1
        alt_pct = 50 + round(t * 50)   # 50 -> 100
        btc_pct = 100 - alt_pct        # 50 -> 0

    alt_pct = max(0, min(100, int(round(alt_pct))))
    btc_pct = 100 - alt_pct
    return f"{alt_pct}/{btc_pct}"


def compute_pot_roi_frac(
    token: str,
    range_str: str,
    btc_mc: float,
    alt_mc: float,
    price_now: float,
) -> Optional[float]:
    """
    Ported from your v1 index.html computePotentialRoi(row, btcMc)

    Returns roi_frac (e.g. 0.19 == +19%) or None.
    """
    if not token:
        return None
    up = str(token).upper()
    if up == "BTC" or "USD" in up:
        return None

    parsed = _parse_range_low_high(range_str or "")
    if not parsed:
        return None
    low, high = parsed

    if not (btc_mc > 0 and alt_mc > 0 and price_now > 0):
        return None

    # neutralHigh = low + 0.60*(high-low)
    neutral_high = low + 0.60 * (high - low)
    dom_target = neutral_high / 100.0
    if not (0 < dom_target < 1):
        return None

    # dom = B/(B+S) -> S = (1-dom)/dom * B
    s_target = (1.0 - dom_target) / dom_target * btc_mc

    supply_est = alt_mc / price_now
    if not (supply_est > 0):
        return None

    target_price = s_target / supply_est
    if not (target_price > 0):
        return None

    roi_frac = target_price / price_now - 1.0
    return roi_frac
