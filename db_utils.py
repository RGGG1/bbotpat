import os
import sqlite3
import time
import json
from typing import Optional, Dict, Any, List, Tuple

DB_DIR = os.path.join(os.path.dirname(__file__), "db")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "hiveai.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    # HMI history
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS hmi_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            hmi REAL NOT NULL,
            band TEXT
        );
        """
    )

    # Hourly prices / dominance table
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS hourly_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            token TEXT NOT NULL,
            price REAL,
            mc REAL,
            btc_dom REAL,
            range_low REAL,
            range_high REAL,
            action TEXT,
            potential_roi REAL
        );
        """
    )

    # KC1 history
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kc1_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            base_balance_usd REAL,
            portfolio_value REAL,
            btc_value REAL
        );
        """
    )

    # KC2 history
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kc2_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            equity_usd REAL,
            roi_frac REAL,
            token TEXT,
            entry_price REAL,
            target_price REAL,
            hmi_override INTEGER,
            bench_btc_roi REAL,
            bench_eth_roi REAL,
            bench_bnb_roi REAL,
            bench_sol_roi REAL
        );
        """
    )

    conn.commit()
    conn.close()


def _normalize_ts(ts_val: Optional[Any]) -> int:
    """
    Normalize various timestamp formats to integer seconds since epoch.
    Accepts:
    - None -> now
    - int/float in ms or seconds
    - ISO string like 2024-11-30T20:00:00Z
    """
    if ts_val is None:
        return int(time.time())

    # Numeric
    if isinstance(ts_val, (int, float)):
        # Heuristic: ms vs s
        if ts_val > 10_000_000_000:  # bigger than year 2286 in seconds, so probably ms
            return int(ts_val / 1000)
        # If it's around typical ms range (> 1e11), also treat as ms
        if ts_val > 1e10:
            return int(ts_val / 1000)
        return int(ts_val)

    # String (try ISO)
    if isinstance(ts_val, str):
        from datetime import datetime
        s = ts_val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            return int(dt.timestamp())
        except Exception:
            return int(time.time())

    return int(time.time())


def log_hmi(hmi: float, band: Optional[str], ts: Optional[Any] = None) -> None:
    ts_norm = _normalize_ts(ts)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO hmi_history (ts, hmi, band) VALUES (?, ?, ?);",
        (ts_norm, float(hmi), band),
    )
    conn.commit()
    conn.close()


def parse_range(range_str: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    if not range_str:
        return (None, None)
    import re

    nums = re.findall(r"(\d+(?:\.\d+)?)", str(range_str))
    if len(nums) < 2:
        return (None, None)
    low = float(nums[0])
    high = float(nums[1])
    if high <= low:
        return (None, None)
    return (low, high)


def log_price_row(ts: Any, row: Dict[str, Any]) -> None:
    ts_norm = _normalize_ts(ts)
    token = str(row.get("token", "")).upper() or "UNKNOWN"

    price = row.get("price")
    mc = row.get("mc")
    btc_dom = row.get("btc_dom")
    rng_low, rng_high = parse_range(row.get("range"))
    action = row.get("action")
    potential_roi = row.get("potential_roi")

    def to_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v))
        except Exception:
            return None

    price = to_float(price)
    mc = to_float(mc)
    btc_dom = to_float(btc_dom)
    potential_roi = to_float(potential_roi)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO hourly_prices (
            ts, token, price, mc, btc_dom, range_low, range_high, action, potential_roi
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (ts_norm, token, price, mc, btc_dom, rng_low, rng_high, action, potential_roi),
    )
    conn.commit()
    conn.close()


def log_kc1(base_balance_usd: Any, portfolio_value: Any, btc_value: Any, ts: Optional[Any] = None) -> None:
    ts_norm = _normalize_ts(ts)

    def to_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v))
        except Exception:
            return None

    base_val = to_float(base_balance_usd)
    port_val = to_float(portfolio_value)
    btc_val = to_float(btc_value)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO kc1_history (
            ts, base_balance_usd, portfolio_value, btc_value
        ) VALUES (?, ?, ?, ?);
        """,
        (ts_norm, base_val, port_val, btc_val),
    )
    conn.commit()
    conn.close()


def log_kc2(
    equity_usd: Any,
    roi_frac: Any,
    token: Optional[str],
    entry_price: Any,
    target_price: Any,
    hmi_override: bool,
    benchmarks: Optional[Dict[str, Any]],
    ts: Optional[Any] = None,
) -> None:
    ts_norm = _normalize_ts(ts)

    def to_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v))
        except Exception:
            return None

    eq = to_float(equity_usd)
    roi = to_float(roi_frac)
    entry = to_float(entry_price)
    target = to_float(target_price)
    token_up = (token or "").upper() or None
    override_int = 1 if hmi_override else 0

    bench_btc = bench_eth = bench_bnb = bench_sol = None
    if benchmarks:
        for sym in ("BTC", "ETH", "BNB", "SOL"):
            row = benchmarks.get(sym)
            if not row:
                continue
            rf = row.get("roi_frac")
            val = to_float(rf)
            if sym == "BTC":
                bench_btc = val
            elif sym == "ETH":
                bench_eth = val
            elif sym == "BNB":
                bench_bnb = val
            elif sym == "SOL":
                bench_sol = val

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO kc2_history (
            ts, equity_usd, roi_frac, token, entry_price, target_price,
            hmi_override, bench_btc_roi, bench_eth_roi, bench_bnb_roi, bench_sol_roi
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            ts_norm,
            eq,
            roi,
            token_up,
            entry,
            target,
            override_int,
            bench_btc,
            bench_eth,
            bench_bnb,
            bench_sol,
        ),
    )
    conn.commit()
    conn.close()


# Initialize schema on import
if __name__ == "__main__":
    init_db()
