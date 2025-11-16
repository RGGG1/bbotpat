#!/usr/bin/env python3
"""
HiveAI Telegram Signal

- Uses:
    - output/fg2_daily.csv            (HMI = FG_lite)
    - output/equity_curve_fg_dom.csv  (equity, btc_only, weights)

- Sends a daily Telegram message with:
    - Top line: HiveAI   dd/mm/yy    💵 $balance_live   ROI: +x.x% (LIVE estimate)
    - HMI value + band + circle emoji
    - BTC vs ALT dominance (ETH+BNB+SOL), plus market caps:
        e.g. 75/25 | $1.9B Vs $650M
    - Prices: BTC, ETH, BNB, SOL
    - Suggested allocation: yesterday%/$ → today%/$ (model, daily close)
    - Action: % of your BTC or ALTs to rotate (model, daily close)
    - HiveAI ROI vs BTC buy & hold (both % and $, model, daily close)
    - HiveAI vs BTC: X.x x multiple (model)
    - HiveAI trades: total days with >1% reallocation since start
"""

import os
import time
from datetime import datetime

import requests
import pandas as pd

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Files produced by your other scripts
HMI_CSV    = "output/fg2_daily.csv"
EQUITY_CSV = "output/equity_curve_fg_dom.csv"

# Starting capital (used for ROI)
INITIAL_CAPITAL = 100.0

# Dominance thresholds (must match backtest_dominance_rotation.py)
DOM_LOW       = 0.75
DOM_HIGH      = 0.81
DOM_MID_LOW   = 0.771
DOM_MID_HIGH  = 0.789
GREED_STABLE_THRESHOLD = 77.0  # HMI >= 77 => full stables in the model

# Assets used in dominance
RISK_IDS  = ["bitcoin", "ethereum", "solana", "binancecoin"]
ALT_IDS   = ["ethereum", "binancecoin", "solana"]
ALT_LABEL = {
    "ethereum": "Eth",
    "binancecoin": "Bnb",
    "solana": "Sol",
}

# Telegram token/chat (support both TELEGRAM_* and TG_*)
TG_TOKEN = (
    os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TG_BOT_TOKEN")
)
TG_CHAT = (
    os.getenv("TELEGRAM_CHAT_ID")
    or os.getenv("TG_CHAT_ID")
)


# ---------- HTTP helpers ----------

def cg_get(path, params=None, max_retries=3, base_sleep=1.0):
    """
    CoinGecko GET with simple retry on 429 (rate limit).
    """
    if params is None:
        params = {}
    url = COINGECKO_BASE + path

    for attempt in range(max_retries):
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            # Rate-limited; back off and retry
            wait = base_sleep * (attempt + 1)
            print(f"[cg_get] 429 Too Many Requests, sleeping {wait:.1f}s then retrying…")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()

    # If we get here, we never succeeded
    raise RuntimeError(f"CoinGecko error {r.status_code}: {r.text[:300]}")


# ---------- HMI helpers ----------

def load_latest_hmi(path=HMI_CSV):
    if not os.path.exists(path):
        raise RuntimeError("HMI CSV not found; run compute_fg2_index.py first.")
    df = pd.read_csv(path, parse_dates=["date"])
    if df.empty:
        raise RuntimeError("HMI CSV is empty.")
    df = df.sort_values("date")
    row = df.iloc[-1]
    return float(row["FG_lite"]), row["date"].date()


def hmi_band_name_and_emoji(hmi: float):
    """
    Your tiers:

    <10      -> Zombie apocalypse
    10-25    -> McDonalds applications
    25-40    -> Ngmi
    40-60    -> Stable
    60-80    -> We're early
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


# ---------- Dominance & prices via /coins/markets ----------

def fetch_markets():
    """
    Fetch /coins/markets once for all RISK_IDS.
    Includes 24h change for live equity marking.
    """
    js = cg_get(
        "/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ",".join(RISK_IDS),
            "per_page": len(RISK_IDS),
            "page": 1,
            "price_change_percentage": "24h",
        },
    )
    return js


def fetch_dominance_and_alt_label(markets):
    """
    Returns (btc_dom_fraction, alt_label_str, btc_mc, alts_mc_sum)

    alt_label_str is concatenation of short names in order of current market cap,
    e.g. 'EthBnbSol'.
    """
    caps = {row["id"]: row["market_cap"] for row in markets}

    btc_mc = caps.get("bitcoin", 0.0)
    alt_caps = [(aid, caps.get(aid, 0.0)) for aid in ALT_IDS]
    alt_caps_sorted = sorted(alt_caps, key=lambda x: x[1], reverse=True)

    alts_mc_sum = sum(mc for _, mc in alt_caps_sorted)

    if btc_mc + alts_mc_sum == 0:
        btc_dom = 0.5
    else:
        btc_dom = btc_mc / (btc_mc + alts_mc_sum)

    label = "".join(ALT_LABEL.get(aid, aid[:3].title()) for aid, _ in alt_caps_sorted)

    return btc_dom, label, btc_mc, alts_mc_sum


def fetch_spot_prices_from_markets(markets):
    """
    Extract BTC, ETH, BNB, SOL prices from the same /coins/markets response.
    """
    prices = {"BTC": None, "ETH": None, "BNB": None, "SOL": None}
    for row in markets:
        cid = row["id"]
        price = row.get("current_price")
        if cid == "bitcoin":
            prices["BTC"] = price
        elif cid == "ethereum":
            prices["ETH"] = price
        elif cid == "binancecoin":
            prices["BNB"] = price
        elif cid == "solana":
            prices["SOL"] = price
    return prices


def format_market_cap(v: float) -> str:
    """
    Format market cap as:
    - >= 1e12 → X.yT
    - >= 1e9  → XXXB (rounded, no decimals)
    - >= 1e6  → XXXM (rounded, no decimals)
    """
    if v is None:
        return "$0"
    if v >= 1e12:
        return f"${v / 1e12:.1f}T"
    if v >= 1e9:
        return f"${int(round(v / 1e9))}B"
    if v >= 1e6:
        return f"${int(round(v / 1e6))}M"
    return f"${int(v):,}"


def compute_live_balance(equity_close, btc_only_close, w_btc, w_alts, w_stb, markets):
    """
    Approximate live portfolio value based on 24h % change from CoinGecko.

    We use:
      ratio = 1 + price_change_percentage_24h / 100

    For ALTs, we average ratios of ETH, BNB, SOL equally.
    Stables assumed ratio = 1.

    Returns:
      equity_live, hive_roi_live_pct
    """
    by_id = {row["id"]: row for row in markets}

    def get_ratio(cid):
        row = by_id.get(cid)
        if not row:
            return 1.0
        ch = row.get("price_change_percentage_24h")
        if ch is None:
            ch = 0.0
        return 1.0 + ch / 100.0

    ratio_btc = get_ratio("bitcoin")
    ratio_eth = get_ratio("ethereum")
    ratio_bnb = get_ratio("binancecoin")
    ratio_sol = get_ratio("solana")

    # equal-weight alt basket ratio
    alt_ratios = [ratio_eth, ratio_bnb, ratio_sol]
    alt_ratio = sum(alt_ratios) / len(alt_ratios)

    # portfolio live factor
    factor_port = w_btc * ratio_btc + w_alts * alt_ratio + w_stb * 1.0

    equity_live = equity_close * factor_port

    hive_roi_live_pct = (equity_live - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100.0

    return equity_live, hive_roi_live_pct


# ---------- Equity & allocations ----------

def load_equity_snapshot(path=EQUITY_CSV):
    """
    Returns (last_row, prev_row) from equity_curve_fg_dom.csv
    where each row has:
        date, equity, btc_only, btc_dom, HMI, w_btc, w_alts, w_stables
    """
    if not os.path.exists(path):
        return None, None
    df = pd.read_csv(path, parse_dates=["date"])
    if df.empty:
        return None, None
    df = df.sort_values("date")
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else None
    return last, prev


def compute_roi_fields(last_row):
    """
    Compute ROI numbers (model / daily close):

    - HiveAI equity & ROI vs 100
    - BTC-only equity & ROI vs 100
    - Outperformance multiple: (HiveAI growth) / (BTC growth)
      where growth = equity / 100
    """
    equity   = float(last_row["equity"])
    btc_only = float(last_row["btc_only"])

    hive_profit = equity - INITIAL_CAPITAL
    btc_profit  = btc_only - INITIAL_CAPITAL

    hive_roi_pct = (hive_profit / INITIAL_CAPITAL) * 100.0
    btc_roi_pct  = (btc_profit / INITIAL_CAPITAL) * 100.0

    if btc_only <= 0:
        outperf = None
    else:
        # growth factor ratio
        outperf = equity / btc_only

    return equity, btc_only, hive_roi_pct, hive_profit, btc_roi_pct, btc_profit, outperf


def describe_allocation_change(prev_row, curr_row):
    """
    From equity_curve_fg_dom, derive yesterday & today allocations.

    Returns:
      summary_text, main_flow, prev_w, curr_w

    main_flow is dict with:
      {
        "src": "BTC"/"ALTs"/"Stables",
        "dst": "BTC"/"ALTs"/"Stables",
        "size_total": <percentage of portfolio moved, approx>,
        "src_key": "btc"/"alts"/"stables",
        "dst_key": ...
      }
    or None if no meaningful change.
    """
    if prev_row is None or curr_row is None:
        return (
            "No prior allocation history.",
            None,
            None,
            {
                "btc": float(curr_row["w_btc"]),
                "alts": float(curr_row["w_alts"]),
                "stables": float(curr_row["w_stables"]),
            } if curr_row is not None else None,
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

    # If all little (<1%), call it "no major" but still return weights
    if all(abs(v) < 1.0 for v in deltas_pct.values()):
        text = (
            f"Yesterday: BTC {prev_w['btc']*100:.1f}%, ALTs {prev_w['alts']*100:.1f}%, "
            f"Stables {prev_w['stables']*100:.1f}%\n"
            f"Today:     BTC {curr_w['btc']*100:.1f}%, ALTs {curr_w['alts']*100:.1f}%, "
            f"Stables {curr_w['stables']*100:.1f}%"
        )
        return text, None, prev_w, curr_w

    # Determine main flow: from the most-negative delta to most-positive
    src_key = min(deltas, key=lambda k: deltas[k])
    dst_key = max(deltas, key=lambda k: deltas[k])

    flow_size_total = min(abs(deltas_pct[src_key]), abs(deltas_pct[dst_key]))
    asset_name_map = {"btc": "BTC", "alts": "ALTs", "stables": "Stables"}

    summary = (
        f"Yesterday: BTC {prev_w['btc']*100:.1f}%, ALTs {prev_w['alts']*100:.1f}%, "
        f"Stables {prev_w['stables']*100:.1f}%\n"
        f"Today:     BTC {curr_w['btc']*100:.1f}%, ALTs {curr_w['alts']*100:.1f}%, "
        f"Stables {curr_w['stables']*100:.1f}%"
    )

    main_flow = {
        "src": asset_name_map[src_key],
        "dst": asset_name_map[dst_key],
        "size_total": flow_size_total,
        "src_key": src_key,
        "dst_key": dst_key,
    }

    return summary, main_flow, prev_w, curr_w


def summarise_trades_total(path=EQUITY_CSV, threshold=0.01):
    """
    Count total 'trade days' where the sum of absolute allocation changes
    (BTC, ALTs, Stables) vs previous day exceeds 'threshold'.

    threshold=0.01 -> about 1% of portfolio moved.
    """
    if not os.path.exists(path):
        return 0

    df = pd.read_csv(path, parse_dates=["date"])
    if df.empty or len(df) < 2:
        return 0

    df = df.sort_values("date")
    total_trades = 0

    prev = df.iloc[0]
    for i in range(1, len(df)):
        curr = df.iloc[i]

        prev_w = (
            float(prev["w_btc"]),
            float(prev["w_alts"]),
            float(prev["w_stables"]),
        )
        curr_w = (
            float(curr["w_btc"]),
            float(curr["w_alts"]),
            float(curr["w_stables"]),
        )

        deltas = [abs(c - p) for c, p in zip(curr_w, prev_w)]
        total_move = sum(deltas)

        if total_move > threshold:
            total_trades += 1

        prev = curr

    return total_trades


# ---------- Main formatting ----------

def format_signal_message():
    # HMI
    hmi, hmi_date = load_latest_hmi()
    band_name, hmi_circle = hmi_band_name_and_emoji(hmi)

    # Equity / ROI (model / close-based)
    last_row, prev_row = load_equity_snapshot()
    if last_row is None:
        raise RuntimeError("equity_curve_fg_dom.csv missing or empty; run backtest_dominance_rotation.py first.")

    (
        equity_close,
        btc_equity_close,
        hive_roi_close_pct,
        hive_profit_close,
        btc_roi_close_pct,
        btc_profit_close,
        outperf_close,
    ) = compute_roi_fields(last_row)

    # Markets (one call): used for dominance + caps + prices + live equity approximation
    markets = fetch_markets()
    btc_dom, alt_label, btc_mc, alts_mc = fetch_dominance_and_alt_label(markets)
    prices = fetch_spot_prices_from_markets(markets)

    # Approximate live balance & live ROI from 24h % changes
    w_btc = float(last_row["w_btc"])
    w_alts = float(last_row["w_alts"])
    w_stb = float(last_row["w_stables"])
    equity_live, hive_roi_live_pct = compute_live_balance(
        equity_close, btc_equity_close, w_btc, w_alts, w_stb, markets
    )

    btc_dom_pct = int(round(btc_dom * 100))
    alt_dom_pct = 100 - btc_dom_pct

    btc_cap_str = format_market_cap(btc_mc)
    alts_cap_str = format_market_cap(alts_mc)

    # Allocation change (yesterday → today) for %/$ block
    flow_summary_text, main_flow, prev_w, curr_w = describe_allocation_change(prev_row, last_row)

    # Top line: HiveAI   dd/mm/yy    💵 $xx.xx   ROI: +x.x%  (LIVE)
    date_str = hmi_date.strftime("%d/%m/%y")
    top_line = f"HiveAI   {date_str}    💵 ${equity_live:,.2f}   ROI: {hive_roi_live_pct:+.1f}%"

    # Suggested allocation: yesterday → today, with $ values (model equity)
    allocation_lines = ["🧠 Suggested allocation:"]

    if curr_w is not None:
        curr_btc_pct = curr_w["btc"] * 100
        curr_alt_pct = curr_w["alts"] * 100
        curr_stb_pct = curr_w["stables"] * 100

        curr_eq = equity_close
        curr_btc_usd = curr_eq * curr_w["btc"]
        curr_alt_usd = curr_eq * curr_w["alts"]
        curr_stb_usd = curr_eq * curr_w["stables"]

        if prev_w is not None and prev_row is not None:
            prev_eq = float(prev_row["equity"])

            prev_btc_pct = prev_w["btc"] * 100
            prev_alt_pct = prev_w["alts"] * 100
            prev_stb_pct = prev_w["stables"] * 100

            prev_btc_usd = prev_eq * prev_w["btc"]
            prev_alt_usd = prev_eq * prev_w["alts"]
            prev_stb_usd = prev_eq * prev_w["stables"]

            allocation_lines.append(
                f" • BTC: {prev_btc_pct:.1f}% → {curr_btc_pct:.1f}% | "
                f"${prev_btc_usd:,.2f} → ${curr_btc_usd:,.2f}"
            )
            allocation_lines.append(
                f" • ALTs: {prev_alt_pct:.1f}% → {curr_alt_pct:.1f}% | "
                f"${prev_alt_usd:,.2f} → ${curr_alt_usd:,.2f}"
            )
            allocation_lines.append(
                f" • Stables: {prev_stb_pct:.1f}% → {curr_stb_pct:.1f}% | "
                f"${prev_stb_usd:,.2f} → ${curr_stb_usd:,.2f}"
            )
        else:
            allocation_lines.append(
                f" • BTC: {curr_btc_pct:.1f}% | ${curr_btc_usd:,.2f}"
            )
            allocation_lines.append(
                f" • ALTs: {curr_alt_pct:.1f}% | ${curr_alt_usd:,.2f}"
            )
            allocation_lines.append(
                f" • Stables: {curr_stb_pct:.1f}% | ${curr_stb_usd:,.2f}"
            )

    allocation_block = "\n".join(allocation_lines)

    # Action line: % of SOURCE asset to move, not % of total
    if main_flow is None or prev_w is None or curr_w is None:
        action_line = "No action."
    else:
        src_key = main_flow["src_key"]   # 'btc'/'alts'/'stables'
        src_label = main_flow["src"]     # 'BTC'/'ALTs'/'Stables'
        dst_label = main_flow["dst"]

        prev_src_pct = prev_w[src_key] * 100.0
        curr_src_pct = curr_w[src_key] * 100.0
        delta_src_pct = abs(curr_src_pct - prev_src_pct)

        if prev_src_pct > 0.1:
            rel_move = (delta_src_pct / prev_src_pct) * 100.0
            action_line = f"Rotate ~{rel_move:.1f}% of your {src_label} to {dst_label}."
        else:
            action_line = f"Rotate allocation toward {dst_label}."

    # ROI text (still close-based model numbers)
    hive_profit_sign = "+" if hive_profit_close >= 0 else "-"
    btc_profit_sign  = "+" if btc_profit_close >= 0 else "-"

    hive_roi_line = f"HiveAI ROI:  {hive_roi_close_pct:+.1f}% / {hive_profit_sign}${abs(hive_profit_close):,.2f}"
    btc_roi_line  = f"BTC buy & hold ROI: {btc_roi_close_pct:+.1f}% / {btc_profit_sign}${abs(btc_profit_close):,.2f}"

    if outperf_close is None:
        outperf_line = "HiveAI vs BTC: n/a"
    else:
        outperf_line = f"HiveAI vs BTC: {outperf_close:.2f}x"

    # Trades
    total_trades = summarise_trades_total()
    trades_line = f"HiveAI trades: {total_trades}"

    # Build full text
    text = (
        f"{top_line}\n\n"
        f"{hmi_circle} HMI: {hmi:.1f} — {band_name}\n"
        f"📊 BTC vs {alt_label}: {btc_dom_pct}/{alt_dom_pct} | {btc_cap_str} Vs {alts_cap_str}\n\n"
        f"💰 Prices:\n"
        f" • BTC: ${prices['BTC']:.0f}\n"
        f" • ETH: ${prices['ETH']:.0f}\n"
        f" • BNB: ${prices['BNB']:.0f}\n"
        f" • SOL: ${prices['SOL']:.2f}\n\n"
        f"{allocation_block}\n\n"
        f"⚙️ Action: {action_line}\n\n"
        f"🧠 {hive_roi_line}\n"
        f"{btc_roi_line}\n"
        f"{outperf_line}\n\n"
        f"{trades_line}"
    )

    return text


# ---------- Telegram send ----------

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
    print("Sent HiveAI message, message_id:", resp.get("result", {}).get("message_id"))


if __name__ == "__main__":
    main()
