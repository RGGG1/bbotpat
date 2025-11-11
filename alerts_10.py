# requirements: pandas, numpy, python-dateutil (optional)
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# -----------------------
# CONFIG
# -----------------------
CONFIG = {
    "coins": ["BTC","ETH","SOL","BNB","XRP"],
    # Daily extreme thresholds that historically give ~90% next-day reversal
    "thresholds_pct": {"BTC":13, "ETH":14, "SOL":17, "BNB":20, "XRP":15},
    # Trading rules
    "leverage": 10.0,
    "sl_underlying": -0.05,   # -5% in underlying -> ~-50% on equity
    "max_hold_days": 4,       # 96 hours
    "use_coin_specific_tp": True,
    # If you want fixed TP instead, set to a float (e.g. 0.065) and set use_coin_specific_tp=False
    "fixed_tp_underlying": None,
}

# -----------------------
# DATA UTILS
# -----------------------
def load_daily_close(path: str) -> pd.DataFrame:
    """
    Accepts .xls/.xlsx/.csv with at least (date/snapped_at) + (price/close).
    Returns daily last close with columns: ['date','price','ret'].
    """
    try:
        if path.lower().endswith(('.xls','.xlsx')):
            df = pd.read_excel(path, sheet_name=0)
        else:
            df = pd.read_csv(path)
    except Exception:
        df = pd.read_html(path)[0]

    cols = {str(c).lower().strip(): c for c in df.columns}
    date_col = cols.get("snapped_at", cols.get("date", list(df.columns)[0]))
    price_col = cols.get("price", cols.get("close", cols.get("adj close", list(df.columns)[1])))

    out = df[[date_col, price_col]].copy()
    out.columns = ["snapped_at","price"]
    out["snapped_at"] = pd.to_datetime(out["snapped_at"], errors="coerce", utc=True)
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna(subset=["snapped_at","price"]).sort_values("snapped_at")

    out["date"] = out["snapped_at"].dt.date
    daily = out.groupby("date", as_index=False).agg(price=("price","last")).copy()
    daily["ret"] = daily["price"].pct_change()
    return daily

def last_n_years(df: pd.DataFrame, years: int = 3) -> pd.DataFrame:
    yrs = pd.to_datetime(df["date"]).dt.year
    return df[yrs >= (yrs.max() - (years - 1))].reset_index(drop=True)

# -----------------------
# SIGNALS
# -----------------------
def build_events(df: pd.DataFrame, threshold_pct: float) -> pd.DataFrame:
    """
    Pick days where |daily % move| >= threshold.
    Returns rows with (entry_idx, date, entry_price, signal_move_%, direction).
    """
    d = df.copy()
    d["ret_pct"] = d["ret"] * 100.0
    rows = []
    for i in range(1, len(d)-1):  # ensure at least one future bar exists
        r = float(d.loc[i, "ret_pct"])
        if abs(r) >= threshold_pct:
            rows.append({
                "date": d.loc[i, "date"],
                "entry_idx": i,
                "entry_price": float(d.loc[i, "price"]),
                "signal_move_%": round(r, 2),
                "direction": "UP" if r > 0 else "DOWN",
            })
    return pd.DataFrame(rows)

def compute_mfe_mae(df: pd.DataFrame, i: int, direction: str, max_days: int = 4):
    """
    MFE/MAE over a max_days window using daily closes.
    direction = 'UP' means contrarian short; 'DOWN' means contrarian long.
    """
    sign = -1 if direction == "UP" else 1  # contrarian
    entry_price = float(df.loc[i, "price"])
    end_idx = min(i + max_days, len(df)-1)
    fav_path = []
    for j in range(i+1, end_idx+1):
        cum = (float(df.loc[j, "price"]) / entry_price) - 1.0
        fav = sign * cum
        fav_path.append(fav)
    if not fav_path:
        return None, None, None
    fav_arr = np.array(fav_path)
    mfe = float(np.max(fav_arr))          # max favorable
    mae = float(np.min(fav_arr))          # most adverse
    t_mfe = int(np.argmax(fav_arr) + 1)   # days to MFE
    return mfe, mae, t_mfe

# -----------------------
# PER-COIN TP DERIVATION (median MFE within 96h)
# -----------------------
def derive_coin_tps(selected_events: pd.DataFrame, df_map: dict, max_hold_days: int = 4) -> dict:
    per_coin_tp = {}
    for coin in selected_events["Coin"].unique():
        dfc = df_map[coin]
        evs = selected_events[selected_events["Coin"] == coin]
        mfes = []
        for _, ev in evs.iterrows():
            i = int(ev["entry_idx"])
            mfe, mae, t_mfe = compute_mfe_mae(dfc, i, ev["direction"], max_days=max_hold_days)
            if mfe is not None:
                mfes.append(mfe)  # fraction (e.g., 0.043)
        if mfes:
            per_coin_tp[coin] = float(np.median(mfes))  # fraction
        else:
            per_coin_tp[coin] = 0.065  # fallback to 6.5% if no data
    return per_coin_tp

# -----------------------
# SIMULATOR (no overlap, 96h cap, SL, TP)
# -----------------------
def simulate_trades(selected_events: pd.DataFrame,
                    df_map: dict,
                    leverage: float = 10.0,
                    sl_underlying: float = -0.05,
                    max_hold_days: int = 4,
                    use_coin_specific_tp: bool = True,
                    fixed_tp_underlying: float | None = None,
                    start_equity: float = 100.0):
    # TPs
    if use_coin_specific_tp:
        coin_tp = derive_coin_tps(selected_events, df_map, max_hold_days=max_hold_days)
    else:
        coin_tp = {c: (fixed_tp_underlying if fixed_tp_underlying is not None else 0.065)
                   for c in df_map.keys()}

    equity = start_equity
    peak = equity
    max_dd = 0.0
    last_exit_date = None
    rows = []

    events_ord = selected_events.sort_values("date").reset_index(drop=True)

    for _, ev in events_ord.iterrows():
        coin = ev["Coin"]
        dfc = df_map[coin]
        i = int(ev["entry_idx"])

        # no overlap (global): skip if next signal date <= last exit date
        if last_exit_date is not None and ev["date"] <= last_exit_date:
            continue

        entry_price = float(ev["entry_price"])
        sign = -1 if ev["direction"] == "UP" else 1
        tp = coin_tp.get(coin, 0.065)  # fraction
        end_idx_cap = min(i + max_hold_days, len(dfc)-1)

        exit_idx, exit_reason = None, None
        for j in range(i+1, end_idx_cap+1):
            cum = (float(dfc.loc[j, "price"]) / entry_price) - 1.0
            fav = sign * cum
            if fav >= tp:
                exit_idx = j; exit_reason = f"TP_{round(tp*100,2)}%"; break
            if fav <= sl_underlying:
                exit_idx = j; exit_reason = "SL_5%"; break
        if exit_idx is None:
            exit_idx = end_idx_cap; exit_reason = "TIME_96h"

        exit_price = float(dfc.loc[exit_idx, "price"])
        fav_exit = sign * ((exit_price / entry_price) - 1.0)
        pnl = leverage * fav_exit
        equity = equity * (1.0 + pnl)

        peak = max(peak, equity)
        dd = equity/peak - 1.0
        max_dd = min(max_dd, dd)

        rows.append({
            "Coin": coin,
            "Entry_Date": str(dfc.loc[i,"date"]),
            "Exit_Date": str(dfc.loc[exit_idx,"date"]),
            "Hold_Days": exit_idx - i,
            "Signal_Move_%": round(ev["signal_move_%"], 2),
            "Direction": ev["direction"],
            "TP_Target_%": round(tp*100.0, 2),
            "Underlying_Move_%": round(fav_exit*100.0, 3),
            "Equity_PnL_%": round(pnl*100.0, 3),
            "Equity_After_$": round(equity, 2),
            "Exit_Reason": exit_reason
        })
        last_exit_date = dfc.loc[exit_idx, "date"]

    trades = pd.DataFrame(rows)
    metrics = {
        "Trades": int(len(trades)),
        "Final_Equity_$": float(round(equity, 2)),
        "Total_Return_%": float(round((equity/100.0 - 1.0)*100.0, 2)),
        "Win_Rate_%": float(round((trades["Equity_PnL_%"] > 0).mean()*100.0, 1)) if len(trades)>0 else None,
        "Median_Trade_%": float(round(trades["Equity_PnL_%"].median(), 2)) if len(trades)>0 else None,
        "Max_Drawdown_%": float(round(max_dd*100.0, 2)),
    }
    return trades, metrics, coin_tp

# -----------------------
# SELECT THE 10 HIGH-CONFIDENCE EVENTS
# -----------------------
def build_10_signals(df_map: dict, thresholds_pct: dict):
    events_all = []
    for coin, dfc in df_map.items():
        ev = build_events(dfc, thresholds_pct[coin])
        if not ev.empty:
            ev["Coin"] = coin
            events_all.append(ev)
    if not events_all:
        return pd.DataFrame(columns=["date","entry_idx","entry_price","signal_move_%","direction","Coin"])
    pooled = pd.concat(events_all, ignore_index=True).sort_values("date")
    # Prefer ETH & SOL first (as before), then fill to 10
    eth_sol = pooled[pooled["Coin"].isin(["ETH","SOL"])]
    if len(eth_sol) >= 10:
        selected = eth_sol.head(10).copy()
    else:
        need = 10 - len(eth_sol)
        rest = pooled[~pooled.index.isin(eth_sol.index)].head(need)
        selected = pd.concat([eth_sol, rest]).sort_values("date").head(10).copy()
    return selected

# -----------------------
# EXAMPLE: run backtest
# -----------------------
if __name__ == "__main__":
    paths = {
        "BTC": "path/to/btc-usd-max.xls",
        "ETH": "path/to/eth-usd-max.xls",
        "SOL": "path/to/sol-usd-max.xls",
        "BNB": "path/to/bnb-usd-max.xls",
        "XRP": "path/to/xrp-usd-max.xls",
    }

    # Load & slice
    df_map = {c: last_n_years(load_daily_close(p), years=3) for c, p in paths.items()}

    # Build the 10-signal set and simulate with coin-specific TP
    selected10 = build_10_signals(df_map, CONFIG["thresholds_pct"])

    trades, metrics, coin_tp = simulate_trades(
        selected10, df_map,
        leverage=CONFIG["leverage"],
        sl_underlying=CONFIG["sl_underlying"],
        max_hold_days=CONFIG["max_hold_days"],
        use_coin_specific_tp=CONFIG["use_coin_specific_tp"],
        fixed_tp_underlying=CONFIG["fixed_tp_underlying"],
        start_equity=100.0
    )

    print("Per-coin TP derived from median MFE within 96h:", {k: round(v*100,3) for k,v in coin_tp.items()})
    print("Metrics:", metrics)
    print(trades.to_string(index=False))
