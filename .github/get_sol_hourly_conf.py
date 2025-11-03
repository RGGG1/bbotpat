#!/usr/bin/env python3
import requests, math
from statistics import mean, pstdev
from datetime import datetime, timezone

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
]
HEADERS = {"User-Agent": "sol-hourly-conf/1.0"}
SYMBOL = "SOLUSDT"
LOOK = 20  # 20-hour lookback for hourly version (same math as daily, just hourly)

def fetch_hourly_closes(limit=60):
    last_err = None
    for base in BASES:
        try:
            url = f"{base}/api/v3/klines"
            # limit >= LOOK+2 to get 1 return for "now" + LOOK history
            r = requests.get(url, params={"symbol": SYMBOL, "interval":"1h", "limit":limit},
                             headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
            closes = [float(k[4]) for k in data]  # close
            close_time = int(data[-1][6]) // 1000
            return closes, close_time
        except Exception as e:
            last_err = e
            continue
    raise last_err if last_err else RuntimeError("All Binance bases failed")

def pct_returns(closes):
    return [closes[i]/closes[i-1]-1.0 for i in range(1, len(closes))]

def main():
    closes, last_ct = fetch_hourly_closes(limit=LOOK+30)
    r = pct_returns(closes)
    if len(r) < LOOK+1:
        raise RuntimeError("Not enough hourly bars")

    last_ret = r[-1]
    window = r[-LOOK-1:-1]  # previous LOOK hourly returns
    mu = mean(window)
    sd = pstdev(window) if len(window) > 1 else 0.0
    z = abs((last_ret - mu) / sd) if sd > 0 else 0.0
    z_signed = z if last_ret > 0 else -z
    level = max(0, min(100, round(50 + 20*z_signed)))

    # Direction and simple label (your bot’s convention)
    direction = "SHORT" if last_ret > 0 else "LONG"
    if level >= 77:
        label = f"Overbought {level}% → {direction} candidate"
    elif level <= 23:
        label = f"Oversold {level}% → {direction} candidate"
    else:
        label = f"Neutral {level}%"

    ts = datetime.fromtimestamp(last_ct, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("=== SOL hourly confidence ===")
    print(f"Bar (last CLOSED 1h): {ts}")
    print(f"Last hourly return: {last_ret*100:.3f}%")
    print(f"z-score (20h window): {z:.3f} (signed {z_signed:.3f})")
    print(f"Confidence level: {level}%  |  Direction: {direction}")
    print(label)

if __name__ == "__main__":
    main()
