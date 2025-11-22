#!/usr/bin/env python3
"""
execute_trades.py

Rebalance a Binance spot account to match portfolio_weights.json
using USDC as the only stable, with an optional BTC shortcut.

Key rules:
- Only trade the algo tokens (from portfolio_weights.json, mapping STABLES -> USDC).
- Spot wallet only.
- All value measured in USDC (1 USDC = 1 USD).
- Min trade size: $5 notional.
- Tolerance after rebalancing: 1% max weight error.
- Routing:
    * Primary: asset <-> USDC (ASSETUSDC, BTCUSDC).
    * Shortcut: if BTC is underweight and a token is overweight, try ASSETBTC.
- Use MARKET orders.
- Try up to MAX_ITER rebalancing passes.
- If trades fail, we log them, continue, and report in Telegram.
"""

import json
import os
import time
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

import hmac
import hashlib
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

ROOT = Path(".")
DOCS = ROOT / "docs"
PW_JSON_ROOT = ROOT / "portfolio_weights.json"
PW_JSON_DOCS = DOCS / "portfolio_weights.json"

STABLE = "USDC"
BASE_URL = "https://api.binance.com"

MIN_TRADE_USD = 1.0          # ignore diffs smaller than this
TARGET_TOL = 0.005            # 1% tolerance
MAX_ITER = 3
SLIPPAGE_BUFFER = 0.001      # +0.1% buffer on BUY notional

TRADES_LOG = Path("/root/trades.log")

load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

if not API_KEY or not API_SECRET:
    raise SystemExit("BINANCE_API_KEY / BINANCE_API_SECRET not set in environment!")

SESSION = requests.Session()
SESSION.headers.update({"X-MBX-APIKEY": API_KEY})


def log(msg: str) -> None:
    line = f"[{datetime.utcnow().isoformat()}Z] {msg}"
    print(line)
    with TRADES_LOG.open("a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------
# Utility: rounding quote amounts
# ---------------------------------------------------------------------

def round_quote(q: float, decimals: int = 2) -> float:
    """
    Round quote amount DOWN to the given decimals (e.g. 2 for USDC),
    to avoid Binance 'too much precision' errors and over-spending balance.
    """
    if q is None:
        return None
    factor = 10 ** decimals
    return math.floor(q * factor) / factor


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------

def send_telegram_message(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code != 200:
            log(f"[tg] sendMessage failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log(f"[tg] Exception sending message: {e}")


# ---------------------------------------------------------------------
# Binance helpers
# ---------------------------------------------------------------------

def _sign_query(params: Dict[str, Any]) -> str:
    query = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return query + f"&signature={signature}"


def binance_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    signed: bool = False,
) -> Any:
    if params is None:
        params = {}
    url = BASE_URL + path
    data = None

    if signed:
        params["timestamp"] = int(time.time() * 1000)
        query = _sign_query(params)
        if method.upper() == "GET":
            url = f"{url}?{query}"
        else:
            data = query
    else:
        if method.upper() == "GET" and params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"

    resp = SESSION.request(method, url, data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Binance error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ---------------------------------------------------------------------
# Portfolio weights & prices
# ---------------------------------------------------------------------

def load_portfolio_weights() -> Dict[str, float]:
    """
    Load target weights from portfolio_weights.json.

    Supports your current format:

        {
          "timestamp": "...",
          "hmi": 60.6,
          "hmi_band": "...",
          "weights": [
            {"asset": "BTC", "weight": 0.2439},
            ...
            {"asset": "STABLES", "weight": 0.1429}
          ]
        }

    And also:
        - { "portfolio_weights": [ { "asset": "...", "weight": ... }, ... ] }
        - [ { "asset": "...", "weight": ... }, ... ]
        - { "BTC": 0.25, "SOL": 0.25, ... }

    Maps STABLES -> USDC and normalises to sum to 1.
    """
    path = PW_JSON_ROOT
    if not path.exists() and PW_JSON_DOCS.exists():
        path = PW_JSON_DOCS
    if not path.exists():
        raise SystemExit("portfolio_weights.json not found in root or docs/")

    raw = json.loads(path.read_text())

    if isinstance(raw, dict) and "weights" in raw:
        rows = raw["weights"]
    elif isinstance(raw, dict) and "portfolio_weights" in raw:
        rows = raw["portfolio_weights"]
    elif isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = [{"asset": k, "weight": v} for k, v in raw.items()]
    else:
        raise SystemExit("Unrecognised portfolio_weights.json format")

    weights: Dict[str, float] = {}
    for row in rows:
        asset = row.get("asset")
        if not asset:
            continue
        try:
            w = float(row.get("weight", 0.0))
        except Exception:
            w = 0.0
        if asset.upper() == "STABLES":
            asset = STABLE
        asset = asset.upper()
        if w <= 0:
            continue
        weights[asset] = weights.get(asset, 0.0) + w

    total = sum(weights.values())
    if total <= 0:
        raise SystemExit("Invalid portfolio_weights.json: total weight <= 0.")
    for k in list(weights.keys()):
        weights[k] = weights[k] / total
    return weights


def fetch_balances(universe: List[str]) -> Dict[str, float]:
    """
    Spot balances for algo tokens only.
    """
    acct = binance_request("GET", "/api/v3/account", signed=True)
    universe_set = set(universe)
    balances: Dict[str, float] = {}
    for bal in acct.get("balances", []):
        asset = bal.get("asset")
        if asset not in universe_set:
            continue
        free = float(bal.get("free", 0.0))
        if free > 0:
            balances[asset] = free
    return balances


def fetch_prices(assets: List[str]) -> Dict[str, float]:
    """
    Price of each algo asset in USDC.
    """
    prices: Dict[str, float] = {}
    for asset in assets:
        if asset == STABLE:
            prices[asset] = 1.0
            continue
        symbol = asset + STABLE
        try:
            data = binance_request(
                "GET", "/api/v3/ticker/price",
                params={"symbol": symbol}, signed=False
            )
            prices[asset] = float(data["price"])
        except Exception as e:
            log(f"[prices] Failed price for {symbol}: {e}")
            prices[asset] = 0.0
    return prices


# ---------------------------------------------------------------------
# Portfolio math
# ---------------------------------------------------------------------

@dataclass
class PortfolioState:
    balances: Dict[str, float]
    prices: Dict[str, float]
    total_value: float
    target_weights: Dict[str, float]
    target_usd: Dict[str, float]
    diffs_usd: Dict[str, float]


def compute_state(universe: List[str], target_weights: Dict[str, float]) -> PortfolioState:
    balances = fetch_balances(universe)
    prices = fetch_prices(universe)
    total = 0.0
    vals: Dict[str, float] = {}
    for a in universe:
        qty = balances.get(a, 0.0)
        p = prices.get(a, 0.0)
        v = qty * p
        vals[a] = v
        total += v
    if total <= 0:
        raise SystemExit("Total algo portfolio value is 0; nothing to rebalance.")

    target_usd: Dict[str, float] = {}
    diffs: Dict[str, float] = {}
    for a in universe:
        w = target_weights.get(a, 0.0)
        t = total * w
        target_usd[a] = t
        diffs[a] = t - vals[a]

    return PortfolioState(
        balances=balances,
        prices=prices,
        total_value=total,
        target_weights=target_weights,
        target_usd=target_usd,
        diffs_usd=diffs,
    )


def weights_from_state(state: PortfolioState) -> Dict[str, float]:
    weights: Dict[str, float] = {}
    for a in state.balances:
        v = state.balances[a] * state.prices.get(a, 0.0)
        if state.total_value > 0:
            weights[a] = v / state.total_value
        else:
            weights[a] = 0.0
    return weights


def max_weight_error(actual: Dict[str, float], target: Dict[str, float]) -> float:
    errs = []
    for a, w_t in target.items():
        w_a = actual.get(a, 0.0)
        errs.append(abs(w_a - w_t))
    return max(errs) if errs else 0.0


# ---------------------------------------------------------------------
# Trade planning
# ---------------------------------------------------------------------

@dataclass
class Trade:
    side: str                  # "BUY" or "SELL"
    symbol: str                # e.g. "SOLUSDC", "SOLBTC"
    quote_order_qty: Optional[float] = None  # USDC amount for BUY
    quantity: Optional[float] = None         # base amount for SELL


def build_trade_plan(state: PortfolioState, universe: List[str]) -> List[Trade]:
    """
    Build a list of trades to move towards target weights.
    Primary routing via USDC; BTC shortcut when BTC is a buyer.
    """
    diffs = dict(state.diffs_usd)
    balances = dict(state.balances)
    prices = state.prices

    sellers = [a for a in universe if diffs.get(a, 0.0) < -MIN_TRADE_USD]
    buyers = [a for a in universe if diffs.get(a, 0.0) > MIN_TRADE_USD]

    trades: List[Trade] = []

    # BTC shortcut: sell overweight tokens directly into BTC if BTC is underweight
    btc_diff = diffs.get("BTC", 0.0)
    if btc_diff > MIN_TRADE_USD:
        remaining_btc_usd_need = btc_diff
        for a in sellers:
            if a in (STABLE, "BTC"):
                continue
            if remaining_btc_usd_need <= MIN_TRADE_USD:
                break

            price_a_usdc = prices.get(a, 0.0)
            price_btc_usdc = prices.get("BTC", 0.0)
            if price_a_usdc <= 0 or price_btc_usdc <= 0:
                continue

            current_qty = balances.get(a, 0.0)
            current_usd = current_qty * price_a_usdc
            if current_usd <= MIN_TRADE_USD:
                continue

            usd_to_sell = min(-diffs[a], remaining_btc_usd_need)
            if usd_to_sell < MIN_TRADE_USD:
                continue

            qty_to_sell = usd_to_sell / price_a_usdc
            if qty_to_sell <= 0:
                continue

            symbol = a + "BTC"
            balances[a] = balances.get(a, 0.0) - qty_to_sell
            btc_gain = qty_to_sell * (price_a_usdc / price_btc_usdc)
            balances["BTC"] = balances.get("BTC", 0.0) + btc_gain

            diffs[a] += usd_to_sell
            diffs["BTC"] -= usd_to_sell
            remaining_btc_usd_need -= usd_to_sell

            trades.append(
                Trade(
                    side="SELL",
                    symbol=symbol,
                    quantity=qty_to_sell,
                )
            )

    # Sell remaining overweight assets into USDC
    for a in sellers:
        d = diffs.get(a, 0.0)
        if d >= -MIN_TRADE_USD:
            continue
        if a == STABLE:
            continue

        price_a_usdc = prices.get(a, 0.0)
        if price_a_usdc <= 0:
            continue

        current_qty = balances.get(a, 0.0)
        current_usd = current_qty * price_a_usdc
        if current_usd <= MIN_TRADE_USD:
            continue

        usd_to_sell = min(-d, current_usd)
        if usd_to_sell < MIN_TRADE_USD:
            continue

        qty_to_sell = usd_to_sell / price_a_usdc
        if qty_to_sell <= 0:
            continue

        symbol = a + STABLE
        balances[a] = balances.get(a, 0.0) - qty_to_sell
        balances[STABLE] = balances.get(STABLE, 0.0) + usd_to_sell

        diffs[a] += usd_to_sell
        diffs[STABLE] -= usd_to_sell

        trades.append(
            Trade(
                side="SELL",
                symbol=symbol,
                quantity=qty_to_sell,
            )
        )

    # Buy underweight assets using USDC
    usdc_qty = balances.get(STABLE, 0.0)
    if usdc_qty > 0:
        for a in buyers:
            if a == STABLE:
                continue
            d = diffs.get(a, 0.0)
            if d <= MIN_TRADE_USD:
                continue

            price_a_usdc = prices.get(a, 0.0)
            if price_a_usdc <= 0:
                continue

            usd_to_spend = min(d, usdc_qty)
            if usd_to_spend < MIN_TRADE_USD:
                continue

            usd_to_spend *= (1 + SLIPPAGE_BUFFER)
            if usd_to_spend > usdc_qty:
                usd_to_spend = usdc_qty

            symbol = a + STABLE
            balances[STABLE] -= usd_to_spend
            est_qty = usd_to_spend / price_a_usdc
            balances[a] = balances.get(a, 0.0) + est_qty

            diffs[a] -= usd_to_spend
            diffs[STABLE] += usd_to_spend
            usdc_qty = balances.get(STABLE, 0.0)

            trades.append(
                Trade(
                    side="BUY",
                    symbol=symbol,
                    quote_order_qty=usd_to_spend,
                )
            )

    return trades


# ---------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------

def place_order(t: Trade) -> Optional[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "symbol": t.symbol,
        "side": t.side,
        "type": "MARKET",
    }

    if t.side == "BUY":
        if t.quote_order_qty is None:
            log(f"[LIVE] BUY {t.symbol} missing quote_order_qty, skipping.")
            return None
        q = round_quote(float(t.quote_order_qty), 2)  # 2 decimals for USDC
        if q <= 0:
            log(f"[LIVE] quoteOrderQty rounded to <= 0 for {t.symbol}, skipping.")
            return None
        params["quoteOrderQty"] = q
    else:
        if t.quantity is None:
            log(f"[LIVE] SELL {t.symbol} missing quantity, skipping.")
            return None
        params["quantity"] = float(t.quantity)

    if not LIVE_TRADING:
        log(f"[DRY-RUN] Would place {t.side} {t.symbol}: {params}")
        return None

    log(f"[LIVE] Placing {t.side} {t.symbol}: {params}")
    try:
        res = binance_request("POST", "/api/v3/order", params=params, signed=True)
        log(f"[LIVE] Order result: {res}")
        return res
    except Exception as e:
        log(f"[LIVE] Order FAILED for {t.symbol}: {e}")
        return None


def run_rebalance() -> None:
    target_weights = load_portfolio_weights()
    universe = sorted(target_weights.keys())
    if STABLE not in universe:
        universe.append(STABLE)

    log(f"[info] LIVE_TRADING={LIVE_TRADING}, STABLE={STABLE}")
    log(f"[info] Universe={universe}")
    log(f"[info] Target weights={target_weights}")

    problems: List[str] = []
    final_state: Optional[PortfolioState] = None

    for iteration in range(1, MAX_ITER + 1):
        log(f"[iter {iteration}] Computing state...")
        state = compute_state(universe, target_weights)
        final_state = state

        actual_weights = weights_from_state(state)
        err = max_weight_error(actual_weights, target_weights)
        log(f"[iter {iteration}] Total value={state.total_value:.2f} USDC, "
            f"max weight error={err:.2%}")

        if err <= TARGET_TOL:
            log(f"[iter {iteration}] Within tolerance; no trades needed.")
            break

        trades = build_trade_plan(state, universe)
        if not trades:
            log(f"[iter {iteration}] No trades generated, but error={err:.2%} > tol.")
            problems.append(
                f"No trades generated at iteration {iteration} with error={err:.2%}"
            )
            break

        log(f"[iter {iteration}] Planned {len(trades)} trades:")
        for t in trades:
            log(f"   {t}")

        for t in trades:
            res = place_order(t)
            if res is None and LIVE_TRADING:
                problems.append(f"Order failed: {t.symbol} {t.side}")

    # Final summary
    if final_state is None:
        return

    final_state = compute_state(universe, target_weights)
    actual_weights = weights_from_state(final_state)
    err = max_weight_error(actual_weights, target_weights)
    log(f"[summary] Final max weight error={err:.2%}")

    lines: List[str] = []
    lines.append("<b>Rebalance summary</b>")
    lines.append(f"Value: <b>{final_state.total_value:.2f} USDC</b>")
    lines.append(f"Max weight error: <b>{err:.2%}</b>")
    lines.append("")
    lines.append("<b>Target vs actual weights:</b>")
    for a in sorted(target_weights.keys()):
        wt = target_weights[a]
        wa = actual_weights.get(a, 0.0)
        lines.append(f"{a}: target {wt:.2%}, actual {wa:.2%}")
    if problems:
        lines.append("")
        lines.append("<b>Issues encountered:</b>")
        for p in problems:
            lines.append(f"- {p}")
    send_telegram_message("\n".join(lines))


def main() -> None:
    log("--- execute_trades.py START ---")
    try:
        run_rebalance()
    except Exception as e:
        log(f"[fatal] Exception in rebalance: {e}")
        send_telegram_message(f"<b>Rebalance fatal error</b>\n{e}")
    log("--- execute_trades.py END ---")


if __name__ == "__main__":
    main()
