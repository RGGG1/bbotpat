#!/usr/bin/env python3
"""
send_fg_dom_signal_telegram.py

Runs twice daily (e.g. 05:15, 17:15 UTC) after HMI has been updated.

Binance + supplies version with "all-or-nothing" writes:

- Load latest HMI from hmi_latest.json (root or docs/)
- Load circulating supplies from supplies_latest.json (root or docs/)
- Use Binance spot API for:
    * live prices and 24h change (ticker/24hr)
    * daily close prices (klines) for BTC + all alts (up to ~730d)
- Compute market caps = price * circulating_supply
- Compute per-token BTC dominance and ~2-yr dominance ranges (BTC vs each alt)
    * range horizon: up to 730 days of overlapping price history
- Compute BTC vs ALL ALTS dominance (excluding stables) + range + days
- For each alt:
    - Map dominance position within its range to
      BTC / ALT / STABLE weights (35% / 30% / 35% bands), respecting HMI
- Combine all per-token weights into one global allocation
    (BTC + each ALT + stables)
- Write:
    - dom_bands_latest.json
    - prices_latest.json
    - portfolio_weights.json
  in both root and docs/
- Send a Telegram message with summary + service health.

Important design choices:

- "All or nothing": if any critical data source fails, we do NOT write
  new JSONs. Old JSONs remain in place; Telegram gets a clear FAILURE
  message.
- Website "Range" column: always simple "min–max%" (no days).
- Portfolio tracker: we simulate a $100 portfolio starting from a fixed
  user-specified allocation, and fully rebalance each successful run
  into the newly suggested weights. We also track a BTC-only $100
  buy-and-hold baseline.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple, List

import requests

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

BINANCE_SPOT = "https://api.binance.com"

ROOT = Path(".")
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True, parents=True)

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

HMI_FILES = [ROOT / "hmi_latest.json", DOCS / "hmi_latest.json"]
SUPPLIES_FILES = [ROOT / "supplies_latest.json", DOCS / "supplies_latest.json"]

DOM_JSON_ROOT = ROOT / "dom_bands_latest.json"
DOM_JSON_DOCS = DOCS / "dom_bands_latest.json"
PRICES_JSON_ROOT = ROOT / "prices_latest.json"
PRICES_JSON_DOCS = DOCS / "prices_latest.json"
PW_JSON_ROOT = ROOT / "portfolio_weights.json"
PW_JSON_DOCS = DOCS / "portfolio_weights.json"

PORTFOLIO_TRACKER_JSON = DATA_DIR / "portfolio_tracker.json"
KNIFECATCHER_JSON_ROOT = ROOT / "knifecatcher_latest.json"
KNIFECATCHER_JSON_DOCS = DOCS / "knifecatcher_latest.json"

TG_TOKEN = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

GREED_STABLE_THRESHOLD = 77.0
DAYS_HISTORY_TARGET = 730

# Maximum times we try to build a full snapshot before giving up
SNAPSHOT_MAX_ATTEMPTS = 3
SNAPSHOT_RETRY_DELAY = 10.0  # seconds

# Symbols and their Binance pairs for USD-equivalent pricing
BINANCE_SYMBOLS: Dict[str, str | None] = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "BNB":  "BNBUSDT",
    "SOL":  "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "TON":  "TONUSDT",
    "SUI":  "SUIUSDT",
    "UNI":  "UNIUSDT",
    "USDT": None,         # treat as $1
    "USDC": "USDCUSDT",
}

# Alts used for BTC vs Alts dominance (exclude stables)
ALTS_FOR_DOM = ["ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]

# Order for website table
DISPLAY_ORDER = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "USDTC", "SUI", "UNI"]

# Initial portfolio weights for $100 – assume we are currently 100% in stables.
# On the first run we treat this as our "starting holdings" and immediately
# rebalance into the recommended weights.
INITIAL_PORTFOLIO_WEIGHTS: Dict[str, float] = {
    "STABLES": 1.0,
}

INITIAL_PORTFOLIO_USD = 100.0


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def bn_spot_get(path: str,
                params: Dict[str, Any] | None = None,
                timeout: int = 30,
                sleep: float = 0.1,
                max_retries: int = 5) -> Any:
    if params is None:
        params = {}
    url = BINANCE_SPOT + path
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if r.status_code == 200:
                time.sleep(sleep)
                return r.json()
            if r.status_code in (429, 418, 500, 502, 503, 504):
                last_err = r.text[:300]
            else:
                raise RuntimeError(f"Binance error {r.status_code}: {r.text[:300]}")
        if attempt < max_retries:
            delay = sleep * attempt
            print(f"[Binance] retry {attempt}/{max_retries} in {delay:.2f}s…")
            time.sleep(delay)
    raise RuntimeError(f"Binance error after retries: {last_err}")


def load_hmi() -> Tuple[float | None, str]:
    for p in HMI_FILES:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                return float(js.get("hmi")), js.get("band", "")
            except Exception:
                continue
    return None, ""


def load_supplies() -> Tuple[Dict[str, float], bool, List[str]]:
    """
    Load circulating supplies from supplies_latest.json.
    Returns (supplies_dict, supplies_ok, missing_list).
    supplies_dict: sym -> circulating_supply (float)
    supplies_ok: True if at least BTC & ETH found.
    """
    for p in SUPPLIES_FILES:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                sup = js.get("supplies", {})
                out: Dict[str, float] = {}
                for sym, entry in sup.items():
                    try:
                        out[sym] = float(entry.get("circulating_supply"))
                    except Exception:
                        continue
                missing = js.get("missing", [])
                ok = "BTC" in out and "ETH" in out
                return out, ok, missing
            except Exception as e:
                print("[supplies] Error parsing", p, ":", e)

    # no file
    return {}, False, []


def fmt_mc(v: float) -> str:
    if v <= 0:
        return "$0"
    if v >= 1e12:
        return f"${v/1e12:.1f}T"
    if v >= 1e9:
        return f"${int(round(v/1e9))}B"
    if v >= 1e6:
        return f"${int(round(v/1e6))}M"
    return f"${int(v):,}"


def weights_from_dom(dom_pct: float,
                     dom_min: float,
                     dom_max: float,
                     hmi: float | None) -> Tuple[float, float, float]:
    """
    Map dominance to (w_btc, w_alt, w_stables) with:
    - lower 35% of range: BTC -> ALTs linearly
    - middle 30%: 100% stables
    - upper 35%: BTC -> ALTs linearly
    HMI override: if hmi >= GREED_STABLE_THRESHOLD => 100% stables.
    """
    if hmi is not None and hmi >= GREED_STABLE_THRESHOLD:
        return 0.0, 0.0, 1.0

    span = dom_max - dom_min
    if span <= 0:
        return 0.0, 0.0, 1.0

    t = (dom_pct - dom_min) / span
    t = max(0.0, min(1.0, t))

    if t < 0.35:
        local = t / 0.35
        w_btc = 1.0 - local
        w_alt = local
        return w_btc, w_alt, 0.0

    if t < 0.65:
        return 0.0, 0.0, 1.0

    local = (t - 0.65) / 0.35
    w_btc = 1.0 - local
    w_alt = local
    return w_btc, w_alt, 0.0


def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        print("[tg] Missing TG token or chat ID, skipping Telegram.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if r.status_code != 200:
            print("[tg] Error:", r.text[:200])
    except Exception as e:
        print("[tg] Exception:", e)


def fetch_price_history(symbol: str,
                        days_limit: int = DAYS_HISTORY_TARGET) -> Dict[datetime.date, float]:
    """
    Fetch up to `days_limit` daily closing prices for a Binance spot symbol,
    using /api/v3/klines with interval=1d and limit=days_limit.

    Returns {date -> close_price}.
    """
    data = bn_spot_get(
        "/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": days_limit},
    )
    out: Dict[datetime.date, float] = {}
    for k in data:
        open_time_ms = k[0]
        close_price = float(k[4])
        d = datetime.utcfromtimestamp(open_time_ms / 1000.0).date()
        out[d] = close_price
    return out


def load_previous_dom_range() -> Tuple[float | None, float | None, int | None]:
    for p in [DOM_JSON_ROOT, DOM_JSON_DOCS]:
        if p.exists():
            try:
                js = json.loads(p.read_text())
                return js.get("min_pct"), js.get("max_pct"), js.get("days")
            except Exception:
                continue
    return None, None, None


# ------------------------------------------------------------------
# Snapshot builder (single attempt)
# ------------------------------------------------------------------

def build_snapshot() -> Dict[str, Any]:
    health: Dict[str, bool] = {
        "hmi_ok": False,
        "binance_ok": True,
        "supplies_ok": False,
        "bands_ok": False,
        "prices_ok": False,
        "json_written": False,
    }

    # 1) HMI
    hmi, hmi_band = load_hmi()
    if hmi is not None:
        health["hmi_ok"] = True

    # 2) Supplies
    supplies, supplies_ok, missing_sup = load_supplies()
    health["supplies_ok"] = supplies_ok
    if not supplies_ok:
        raise RuntimeError("Supplies file missing core symbols (BTC/ETH).")

    # 3) Previous aggregate dom range (in case of zero-history fallback)
    prev_min_pct, prev_max_pct, prev_days_all = load_previous_dom_range()

    # 4) Live prices & tickers from Binance
    try:
        ticker_list = bn_spot_get("/api/v3/ticker/24hr")
        by_symbol = {row["symbol"]: row for row in ticker_list}
    except Exception as e:
        health["binance_ok"] = False
        raise RuntimeError(f"Binance ticker error: {e}")

    def live_price(sym: str) -> float:
        if sym in ("USDT", "USDTC"):
            return 1.0
        bsym = BINANCE_SYMBOLS.get(sym)
        if not bsym:
            return 0.0
        row = by_symbol.get(bsym)
        if not row:
            return 0.0
        try:
            return float(row["lastPrice"])
        except Exception:
            return 0.0

    def live_change_24h(sym: str) -> float:
        if sym in ("USDT", "USDTC"):
            return 0.0
        bsym = BINANCE_SYMBOLS.get(sym)
        if not bsym:
            return 0.0
        row = by_symbol.get(bsym)
        if not row:
            return 0.0
        try:
            return float(row.get("priceChangePercent", 0.0))
        except Exception:
            return 0.0

    def supply(sym: str) -> float:
        return float(supplies.get(sym, 0.0))

    def mc_live(sym: str) -> float:
        if sym == "USDTC":
            return supply("USDT") * 1.0 + supply("USDC") * 1.0
        p = live_price(sym)
        s = supply(sym)
        return p * s

    # live MCs
    btc_mc_now = mc_live("BTC")
    alt_mc_now_map = {sym: mc_live(sym) for sym in ALTS_FOR_DOM}
    alt_mc_now_total = sum(alt_mc_now_map.values())

    # 5) Historical price series (close prices) for BTC + alts
    btc_hist: Dict[datetime.date, float] = {}
    alt_histories: Dict[str, Dict[datetime.date, float]] = {}

    try:
        bsym_btc = BINANCE_SYMBOLS["BTC"]
        btc_hist = fetch_price_history(bsym_btc, days_limit=DAYS_HISTORY_TARGET)
        for sym in ALTS_FOR_DOM:
            bsym_alt = BINANCE_SYMBOLS.get(sym)
            if not bsym_alt:
                alt_histories[sym] = {}
                continue
            alt_histories[sym] = fetch_price_history(
                bsym_alt, days_limit=DAYS_HISTORY_TARGET
            )
    except Exception as e:
        health["binance_ok"] = False
        raise RuntimeError(f"Binance klines error: {e}")

    # 6) Per-token dominance and weights

    per_token_dom: Dict[str, Tuple[float | None, float | None, float | None]] = {}
    per_token_weights: Dict[str, Tuple[float, float, float]] = {}
    per_token_days: Dict[str, int] = {}

    for sym in ALTS_FOR_DOM:
        s_btc = supply("BTC")
        s_alt = supply(sym)
        if s_btc <= 0 or s_alt <= 0:
            raise RuntimeError(f"Missing supply for BTC or {sym}")

        dom_series: List[float] = []
        btc_prices = btc_hist or {}
        alt_prices = alt_histories.get(sym, {}) or {}

        for d, p_btc in btc_prices.items():
            p_alt = alt_prices.get(d)
            if p_alt is None:
                continue
            mc_btc_d = p_btc * s_btc
            mc_alt_d = p_alt * s_alt
            tot = mc_btc_d + mc_alt_d
            if tot <= 0:
                continue
            dom_series.append(100.0 * mc_btc_d / tot)

        days_count = len(dom_series)

        # current dominance now
        mc_btc_now_sym = btc_mc_now
        mc_alt_now_sym = alt_mc_now_map.get(sym, 0.0)
        tot_now = mc_btc_now_sym + mc_alt_now_sym
        dom_now = 100.0 * mc_btc_now_sym / tot_now if tot_now > 0 else None

        if days_count <= 0 or dom_now is None:
            raise RuntimeError(f"Invalid dominance history for {sym} (days={days_count})")

        dom_min = min(dom_series)
        dom_max = max(dom_series)

        w_btc, w_alt, w_st = weights_from_dom(dom_now, dom_min, dom_max, hmi)

        per_token_dom[sym] = (dom_now, dom_min, dom_max)
        per_token_weights[sym] = (w_btc, w_alt, w_st)
        per_token_days[sym] = days_count

    # 7) Aggregate BTC vs ALL ALTS dominance range (exclude stables)

    s_btc = supply("BTC")
    alt_supplies = {sym: supply(sym) for sym in ALTS_FOR_DOM}
    alt_hist_total: Dict[datetime.date, float] = {}

    for sym in ALTS_FOR_DOM:
        s_alt = alt_supplies.get(sym, 0.0)
        if s_alt <= 0:
            continue
        for d, p_alt in (alt_histories.get(sym, {}) or {}).items():
            alt_hist_total.setdefault(d, 0.0)
            alt_hist_total[d] += p_alt * s_alt

    dom_all_series: List[float] = []
    btc_prices = btc_hist or {}

    for d, p_btc in btc_prices.items():
        mc_btc_d = p_btc * s_btc
        mc_alt_d = alt_hist_total.get(d, 0.0)
        tot = mc_btc_d + mc_alt_d
        if tot <= 0:
            continue
        dom_all_series.append(100.0 * mc_btc_d / tot)

    days_all = len(dom_all_series)

    if btc_mc_now + alt_mc_now_total > 0:
        btc_dom_all_now = 100.0 * btc_mc_now / (btc_mc_now + alt_mc_now_total)
        alt_dom_all_now = 100.0 - btc_dom_all_now
    else:
        btc_dom_all_now = 50.0
        alt_dom_all_now = 50.0

    if days_all > 0:
        dom_all_min = min(dom_all_series)
        dom_all_max = max(dom_all_series)
    else:
        if prev_min_pct is not None and prev_max_pct is not None:
            dom_all_min = float(prev_min_pct)
            dom_all_max = float(prev_max_pct)
            days_all = prev_days_all or 0
        else:
            dom_all_min = dom_all_max = btc_dom_all_now

    # When we reach here, prices & bands are considered valid
    health["prices_ok"] = True
    health["bands_ok"] = True

    snapshot: Dict[str, Any] = {
        "hmi": hmi,
        "hmi_band": hmi_band,
        "health": health,
        "supplies": supplies,
        "btc_mc_now": btc_mc_now,
        "alt_mc_now_total": alt_mc_now_total,
        "btc_dom_all_now": btc_dom_all_now,
        "alt_dom_all_now": alt_dom_all_now,
        "dom_all_min": dom_all_min,
        "dom_all_max": dom_all_max,
        "days_all": days_all,
        "per_token_dom": per_token_dom,
        "per_token_weights": per_token_weights,
        "per_token_days": per_token_days,
        "btc_hist": btc_hist,
        "alt_histories": alt_histories,
        "live_price_func": live_price,
        "live_change_func": live_change_24h,
        "mc_live_func": mc_live,
    }

    return snapshot


# ------------------------------------------------------------------
# Portfolio tracker
# ------------------------------------------------------------------

def update_portfolio_tracker(
    prices_rows: List[Dict[str, Any]],
    portfolio_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Track a synthetic portfolio that starts fully in STABLES (USDC) and
    rebalances into the model portfolio at each DOM run.

    Also track a BTC buy-and-hold baseline and export a lightweight summary
    for the website (knifecatcher_latest.json).
    """
    INITIAL_PORTFOLIO_USD = 100.0

    now = datetime.utcnow().isoformat() + "Z"

    # Build latest prices map (symbol -> price)
    latest_prices: Dict[str, float] = {}
    for row in prices_rows:
        sym = row.get("symbol")
        try:
            price = float(row.get("price", 0.0))
        except Exception:
            price = 0.0
        if sym and price > 0:
            latest_prices[sym] = price

    # Build latest portfolio weights map (asset -> weight)
    latest_weights: Dict[str, float] = {}
    for row in portfolio_rows:
        asset = row.get("asset")
        try:
            w = float(row.get("weight", 0.0))
        except Exception:
            w = 0.0
        if not asset or w <= 0:
            continue
        if asset.upper() == "STABLES":
            asset = "USDC"
        asset = asset.upper()
        latest_weights[asset] = latest_weights.get(asset, 0.0) + w

    total_w = sum(latest_weights.values())
    if total_w > 0:
        for k in list(latest_weights.keys()):
            latest_weights[k] = latest_weights[k] / total_w

    # Load existing tracker state if present
    if PORTFOLIO_TRACKER_JSON.exists():
        try:
            state = json.loads(PORTFOLIO_TRACKER_JSON.read_text())
        except Exception:
            state = {}
    else:
        state = {}

    # Initialise on first run
    if not state:
        base_timestamp = now
        state = {
            "base_balance_usd": INITIAL_PORTFOLIO_USD,
            "base_timestamp": base_timestamp,
            "holdings": {
                "USDC": INITIAL_PORTFOLIO_USD  # start fully in stable
            },
            "btc_holdings": {
                "BTC": 0.0,
                "USDC": INITIAL_PORTFOLIO_USD,
            },
            "last_value_usd": INITIAL_PORTFOLIO_USD,
            "last_timestamp": now,
            "runs": 0,
        }
        first_run = True
    else:
        first_run = False

    # Current synthetic holdings for the algo
    holdings: Dict[str, float] = state.get("holdings", {})
    holdings = {k.upper(): float(v) for k, v in holdings.items()}

    # BTC buy-and-hold baseline holdings
    btc_holdings: Dict[str, float] = state.get("btc_holdings", {})
    btc_holdings = {k.upper(): float(v) for k, v in btc_holdings.items()}

    # Compute current portfolio value in USDC
    def value_in_usdc(h: Dict[str, float]) -> float:
        total = 0.0
        for asset, qty in h.items():
            asset = asset.upper()
            if asset == "USDC":
                total += qty
            else:
                sym = asset + "USDC"
                px = latest_prices.get(sym, 0.0)
                total += qty * px
        return total

    portfolio_value = value_in_usdc(holdings)

    # BTC baseline: if no BTC yet, invest all USDC into BTC at first run
    if btc_holdings.get("BTC", 0.0) == 0.0:
        usdc_amt = btc_holdings.get("USDC", 0.0)
        px_btc = latest_prices.get("BTCUSDC", 0.0)
        if usdc_amt > 0 and px_btc > 0:
            qty_btc = usdc_amt / px_btc
            btc_holdings["BTC"] = qty_btc
            btc_holdings["USDC"] = 0.0

    btc_value = value_in_usdc(btc_holdings)

    # Rebalance algo synthetic holdings to latest_weights
    total_val = portfolio_value if portfolio_value > 0 else state.get("base_balance_usd", INITIAL_PORTFOLIO_USD)
    new_holdings: Dict[str, float] = {}

    for asset, w in latest_weights.items():
        target_val = total_val * w
        if asset == "USDC":
            new_holdings["USDC"] = new_holdings.get("USDC", 0.0) + target_val
        else:
            sym = asset + "USDC"
            px = latest_prices.get(sym, 0.0)
            if px > 0:
                qty = target_val / px
                new_holdings[asset] = new_holdings.get(asset, 0.0) + qty

    # Save updated state
    state["holdings"] = new_holdings
    state["btc_holdings"] = btc_holdings
    state["last_value_usd"] = portfolio_value
    state["last_timestamp"] = now
    state["runs"] = int(state.get("runs", 0)) + 1
    PORTFOLIO_TRACKER_JSON.write_text(json.dumps(state, indent=2))

    # Build lightweight summary for website
    try:
        tracker_state = json.loads(PORTFOLIO_TRACKER_JSON.read_text())
    except Exception:
        tracker_state = {}

    try:
        base_balance = float(tracker_state.get("base_balance_usd", INITIAL_PORTFOLIO_USD))
    except Exception:
        base_balance = float(INITIAL_PORTFOLIO_USD)

    try:
        port_val = float(portfolio_value)
    except Exception:
        port_val = 0.0

    try:
        btc_val = float(btc_value)
    except Exception:
        btc_val = 0.0

    try:
        runs_val = int(tracker_state.get("runs", 0))
    except Exception:
        runs_val = 0

    base_ts = tracker_state.get("base_timestamp", "")

    def _safe_roi(value: float, base: float) -> float | None:
        try:
            if base <= 0:
                return None
            return (value / base) - 1.0
        except Exception:
            return None

    algo_roi = _safe_roi(port_val, base_balance)
    btc_roi = _safe_roi(btc_val, base_balance)

    kc_payload = {
        "timestamp": now,
        "base_balance_usd": base_balance,
        "portfolio_value": port_val,
        "btc_value": btc_val,
        "algo_roi": algo_roi,
        "btc_roi": btc_roi,
        "runs": runs_val,
        "base_timestamp": base_ts,
    }
    try:
        text_kc = json.dumps(kc_payload, indent=2)
        KNIFECATCHER_JSON_ROOT.write_text(text_kc)
        KNIFECATCHER_JSON_DOCS.write_text(text_kc)
    except Exception as _e:
        print(f"[portfolio_tracker] Failed to write knifecatcher JSON: {_e}")

    return {
        "ok": True,
        "first_run": first_run,
        "portfolio_value": portfolio_value,
        "btc_value": btc_value,
        "base_timestamp": state.get("base_timestamp", ""),
        "runs": int(state.get("runs", 0)),
    }


# ------------------------------------------------------------------
# Output writers
# ------------------------------------------------------------------

def write_outputs(snapshot: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """
    Given a fully built snapshot, writes dom/prices/portfolio JSONs
    and returns:
      - dom_payload (dict)
      - portfolio_rows (list)
      - tracker_info (dict) for Telegram display
    """
    hmi = snapshot["hmi"]
    hmi_band = snapshot["hmi_band"]
    health = snapshot["health"]

    btc_mc_now = snapshot["btc_mc_now"]
    alt_mc_now_total = snapshot["alt_mc_now_total"]
    btc_dom_all_now = snapshot["btc_dom_all_now"]
    alt_dom_all_now = snapshot["alt_dom_all_now"]
    dom_all_min = snapshot["dom_all_min"]
    dom_all_max = snapshot["dom_all_max"]
    days_all = snapshot["days_all"]
    per_token_dom = snapshot["per_token_dom"]
    per_token_days = snapshot["per_token_days"]
    per_token_weights = snapshot["per_token_weights"]

    live_price = snapshot["live_price_func"]
    live_change_24h = snapshot["live_change_func"]
    mc_live = snapshot["mc_live_func"]

    # 1) prices_latest.json rows for website

    rows: List[Dict[str, Any]] = []

    mc_usdt = mc_live("USDT")
    mc_usdc = mc_live("USDC")
    mc_usdtc = mc_usdt + mc_usdc

    for sym in DISPLAY_ORDER:
        if sym == "USDTC":
            price_val = 1.0
            mc_val = mc_usdtc
            change_val = 0.0
            dom_now_display = None
            rng_str = ""
        elif sym == "BTC":
            price_val = live_price(sym)
            mc_val = btc_mc_now
            change_val = live_change_24h(sym)
            dom_now_display = None
            rng_str = ""
        else:
            price_val = live_price(sym)
            mc_val = mc_live(sym)
            change_val = live_change_24h(sym)

            dom_info = per_token_dom.get(sym)
            days_count = per_token_days.get(sym, 0)

            if not dom_info:
                raise RuntimeError(f"Missing dominance info for {sym}")
            dom_now, dom_min, dom_max = dom_info
            if dom_now is None or days_count <= 0:
                raise RuntimeError(f"Invalid dominance history for {sym} (days={days_count})")

            mn_i = round(dom_min)
            mx_i = round(dom_max)
            dom_now_display = dom_now
            rng_str = f"{mn_i}–{mx_i}%"

        rows.append({
            "token": sym,
            "price": price_val,
            "mc": mc_val,
            "change_24h": change_val,
            "btc_dom": round(dom_now_display, 1) if dom_now_display is not None else None,
            "range": rng_str,
        })

    prices_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "health": health,
        "rows": rows,
    }
    text_prices = json.dumps(prices_payload, indent=2)
    PRICES_JSON_ROOT.write_text(text_prices)
    PRICES_JSON_DOCS.write_text(text_prices)

    # 2) dom_bands_latest.json

    agg_mn_i = round(dom_all_min)
    agg_mx_i = round(dom_all_max)

    if days_all <= 0 or dom_all_max <= dom_all_min:
        w_btc_all, w_alt_all, w_st_all = (0.0, 0.0, 1.0)
    else:
        w_btc_all, w_alt_all, w_st_all = weights_from_dom(
            btc_dom_all_now, dom_all_min, dom_all_max, hmi
        )

    if w_st_all > max(w_btc_all, w_alt_all):
        agg_action = "Stable up"
    elif w_btc_all >= w_alt_all:
        agg_action = "Buy BTC"
    else:
        agg_action = "Buy Alts"

    dom_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "btc_pct": round(btc_dom_all_now, 1),
        "alt_pct": round(alt_dom_all_now, 1),
        "min_pct": float(agg_mn_i),
        "max_pct": float(agg_mx_i),
        "days": days_all,
        "btc_mc_fmt": fmt_mc(btc_mc_now),
        "alt_mc_fmt": fmt_mc(alt_mc_now_total),
        "action": agg_action,
    }
    text_dom = json.dumps(dom_payload, indent=2)
    DOM_JSON_ROOT.write_text(text_dom)
    DOM_JSON_DOCS.write_text(text_dom)

    # 3) Combine mini-portfolios into global allocation

    global_weights: Dict[str, float] = {"BTC": 0.0, "STABLES": 0.0}
    for sym in ALTS_FOR_DOM:
        global_weights[sym] = 0.0

    slot_count = len(ALTS_FOR_DOM) or 1

    for sym in ALTS_FOR_DOM:
        w_btc, w_alt, w_st = per_token_weights.get(sym, (0.0, 0.0, 1.0))
        slot_factor = 1.0 / slot_count
        global_weights["BTC"] += w_btc * slot_factor
        global_weights["STABLES"] += w_st * slot_factor
        global_weights[sym] += w_alt * slot_factor

    total = sum(global_weights.values())
    if total <= 0:
        global_weights = {k: (1.0 if k == "STABLES" else 0.0) for k in global_weights}
        total = 1.0

    for k in list(global_weights.keys()):
        global_weights[k] = global_weights[k] / total

    portfolio_rows: List[Dict[str, Any]] = []
    for k in ["BTC"] + ALTS_FOR_DOM + ["STABLES"]:
        if k not in global_weights:
            continue
        portfolio_rows.append({
            "asset": k,
            "weight": round(global_weights[k], 4),
        })

    pw_payload = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "hmi": hmi,
        "hmi_band": hmi_band,
        "weights": portfolio_rows,
    }
    text_pw = json.dumps(pw_payload, indent=2)
    PW_JSON_ROOT.write_text(text_pw)
    PW_JSON_DOCS.write_text(text_pw)

    # Mark JSONs as written
    health["json_written"] = True

    # 4) Update portfolio tracker
    tracker_info = update_portfolio_tracker(rows, portfolio_rows)

    return dom_payload, portfolio_rows, tracker_info


# ------------------------------------------------------------------
# Main orchestration
# ------------------------------------------------------------------

def main() -> None:
    attempt = 1
    last_error = ""
    snapshot: Dict[str, Any] | None = None
    dom_payload: Dict[str, Any] | None = None
    portfolio_rows: List[Dict[str, Any]] = []
    tracker_info: Dict[str, Any] = {}

    while attempt <= SNAPSHOT_MAX_ATTEMPTS:
        try:
            snapshot = build_snapshot()
            dom_payload, portfolio_rows, tracker_info = write_outputs(snapshot)
            break
        except Exception as e:
            last_error = str(e)
            print(f"[dom] Attempt {attempt} failed: {last_error}")
            if attempt >= SNAPSHOT_MAX_ATTEMPTS:
                snapshot = None
                break
            attempt += 1
            time.sleep(SNAPSHOT_RETRY_DELAY)

    if snapshot is None or dom_payload is None:
        # Hard failure: do not overwrite JSONs; send FAIL Telegram
        msg_lines = [
            "<b>HiveAI Rotation Update</b>",
            "",
            "<b>STATUS: FAIL</b>",
            f"Reason: {last_error or 'Unknown error building dominance snapshot.'}",
            "",
            "No JSONs were overwritten; site is still serving previous data.",
        ]
        tg_send("\n".join(msg_lines))
        raise SystemExit(1)

    # Success path: build Telegram message
    hmi = snapshot["hmi"]
    hmi_band = snapshot["hmi_band"]
    health = snapshot["health"]

    def pct_str(x: float) -> str:
        return f"{x*100:.1f}%"

    lines: List[str] = []
    lines.append("<b>HiveAI Rotation Update</b>")
    lines.append("")

    # HMI
    if hmi is not None:
        lines.append(f"HMI: <b>{hmi:.1f}</b> ({hmi_band})")
    else:
        lines.append("HMI: unavailable")

    # BTC vs Alts dominance
    agg_mn_i = int(dom_payload["min_pct"])
    agg_mx_i = int(dom_payload["max_pct"])
    days_all = dom_payload["days"]
    btc_pct = dom_payload["btc_pct"]
    action = dom_payload["action"]

    if days_all > 0:
        lines.append(
            f"BTC vs Alts: <b>{btc_pct:.1f}%</b> "
            f"(range {agg_mn_i}–{agg_mx_i}% over {days_all}d)"
        )
    else:
        lines.append(
            f"BTC vs Alts: <b>{btc_pct:.1f}%</b> "
            f"(range {agg_mn_i}–{agg_mx_i}%, 0d)"
        )
    lines.append(f"Action: <b>{action}</b>")
    lines.append("")

    # Portfolio weights
    lines.append("<b>Portfolio weights</b>:")
    for row in portfolio_rows:
        lines.append(f"{row['asset']}: {pct_str(row['weight'])}")

    # Portfolio tracker comparison
    if tracker_info.get("ok"):
        runs = tracker_info.get("runs", 0)
        base_ts = tracker_info.get("base_timestamp", "")
        port_val = tracker_info.get("portfolio_value", 0.0)
        btc_val = tracker_info.get("btc_value", 0.0)

        try:
            port_val_str = f"${port_val:.2f}"
            btc_val_str = f"${btc_val:.2f}"
        except Exception:
            port_val_str = f"${port_val}"
            btc_val_str = f"${btc_val}"

        lines.append("")
        lines.append("<b>Tracked $100 portfolio</b>:")
        if base_ts:
            lines.append(f"Since: {base_ts}")
        lines.append(f"Algo portfolio: <b>{port_val_str}</b>")
        lines.append(f"BTC-only (HODL): <b>{btc_val_str}</b>")
        lines.append(f"Rebalances applied: {runs}")


    # Service health, explicitly flagging JSONs
    lines.append("")
    lines.append("<b>Service health</b>:")
    lines.append(f"HMI JSON present: {'yes' if health.get('hmi_ok') else 'no'}")
    lines.append(f"Supplies file: {'yes' if health.get('supplies_ok') else 'no'}")
    lines.append(f"Binance data: {'yes' if health.get('binance_ok') else 'no'}")
    lines.append(f"Dominance bands OK: {'yes' if health.get('bands_ok') else 'no'}")
    lines.append(f"Prices OK: {'yes' if health.get('prices_ok') else 'no'}")
    lines.append(f"Current run wrote JSONs: {'yes' if health.get('json_written') else 'no'}")

    print("[dom] Updated dom_bands_latest.json, prices_latest.json, portfolio_weights.json")


if __name__ == "__main__":
    main()
