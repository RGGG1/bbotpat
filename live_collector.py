#!/usr/bin/env python3
import asyncio, json, os, time, math, sqlite3
from datetime import datetime, timezone
import aiohttp
import websockets

# ---- CONFIG ----
LIVE_DIR = os.path.join(os.path.dirname(__file__), "docs_live")
DB_PATH = os.path.join(os.path.dirname(__file__), "db", "hiveai.db")

TOKENS = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]
SYMBOLS = {t: f"{t.lower()}usdt" for t in TOKENS}  # binance spot symbols

# CoinGecko circulating supply file produced by your existing pipeline:
SUPPLIES_JSON = os.path.join(os.path.dirname(__file__), "supplies_latest.json")

# Update cadence for writing JSON snapshots
WRITE_EVERY_SECONDS = 10  # change to 60 if you want strictly 1-min

# ---- helpers ----
def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def safe_float(x, default=None):
    try:
        f = float(x)
        if math.isfinite(f):
            return f
    except:
        pass
    return default

def load_supplies():
    """
    Expecting your existing supplies_latest.json structure:
    { "timestamp": "...", "supplies": { "BTC": { "circulating_supply": ... }, ... } }
    """
    try:
        with open(SUPPLIES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("supplies", {}) or {}
    except:
        return {}

def get_supply(supplies, token):
    entry = (supplies or {}).get(token)
    if not entry:
        return None
    return safe_float(entry.get("circulating_supply"))

def ensure_dirs():
    os.makedirs(LIVE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS live_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        token TEXT NOT NULL,
        price REAL,
        change24 REAL
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS live_dom (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        token TEXT NOT NULL,
        btc_dom REAL,
        range_low REAL,
        range_high REAL
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS live_hmi (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        hmi REAL,
        band TEXT
      )
    """)
    con.commit()
    con.close()

def write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)

# ---- HMI (simple live version) ----
# You can swap this out to match your repo’s exact HMI math.
# For now: we’ll reuse your *existing* hmi_latest.json if present,
# and otherwise compute a placeholder HMI from volatility proxy.
def hmi_band_label(hmi):
    if hmi < 10: return "Zombie Apocalypse"
    if hmi < 25: return "McDonald's Applications in high demand"
    if hmi < 45: return "NGMI"
    if hmi < 50: return "Leaning bearish"
    if hmi < 55: return "Cautiously bullish"
    if hmi < 75: return "It's digital gold"
    if hmi < 90: return "Frothy"
    return "It's the future of finance"

# ---- live state ----
STATE = {
    "prices": {t: None for t in TOKENS},
    "change24": {t: None for t in TOKENS},
    "last_write": 0.0,
}

async def ws_spot_ticker():
    """
    Subscribe to miniTicker stream for all chosen symbols.
    Provides: last price, 24h change (close-open)/open approx via miniTicker fields.
    """
    streams = "/".join([f"{SYMBOLS[t]}@miniTicker" for t in TOKENS])
    url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                async for msg in ws:
                    j = json.loads(msg)
                    data = j.get("data", {})
                    sym = data.get("s", "").lower()
                    last = safe_float(data.get("c"))
                    open_ = safe_float(data.get("o"))
                    if not sym or last is None:
                        continue
                    token = None
                    for t, s in SYMBOLS.items():
                        if s == sym:
                            token = t
                            break
                    if not token:
                        continue

                    STATE["prices"][token] = last
                    if open_ and open_ > 0:
                        STATE["change24"][token] = (last / open_) - 1.0
        except Exception as e:
            print("WS error (ticker):", e)
            await asyncio.sleep(3)

def compute_rows_and_dom(supplies):
    """
    Build rows similar to your existing prices_latest.json rows
    and compute BTC dom vs each token using market caps.
    """
    # market caps
    mcs = {}
    for t in TOKENS:
        px = STATE["prices"].get(t)
        sup = get_supply(supplies, t)
        if px is None or sup is None or sup <= 0:
            mcs[t] = None
        else:
            mcs[t] = px * sup

    btc_mc = mcs.get("BTC")
    rows = []

    for t in TOKENS:
        price = STATE["prices"].get(t)
        mc = mcs.get(t)
        chg = STATE["change24"].get(t)

        btc_dom = None
        if t != "BTC" and btc_mc and mc and (btc_mc + mc) > 0:
            btc_dom = (btc_mc / (btc_mc + mc)) * 100.0

        # range: keep your existing “slow” range file if you want;
        # here we just leave placeholder until you wire it.
        range_str = "–"

        rows.append({
            "token": t,
            "price": price,
            "mc": mc,
            "btc_dom": btc_dom,
            "range": range_str,
            "change_24h": chg
        })
    return rows

def compute_hmi_from_live():
    """
    If you want minute-by-minute HMI, you should port your repo’s HMI function here.
    For now we just read your existing hmi_latest.json if it exists;
    otherwise: use BTC 24h change magnitude as a cheap “fear/greed” proxy.
    """
    # try existing file (keeps compatibility)
    base = os.path.join(os.path.dirname(__file__), "hmi_latest.json")
    try:
        with open(base, "r", encoding="utf-8") as f:
            j = json.load(f)
        hmi = safe_float(j.get("hmi"))
        if hmi is not None:
            return hmi, hmi_band_label(hmi)
    except:
        pass

    # fallback proxy
    chg = STATE["change24"].get("BTC")
    if chg is None:
        return None, "unavailable"

    # map abs change to fear/greed-ish
    # low vol => neutral, big move => extremes
    v = min(0.15, abs(chg)) / 0.15  # 0..1
    hmi = 50 + (chg * 100)  # crude
    hmi = max(0, min(100, hmi))
    return hmi, hmi_band_label(hmi)

def db_insert_snapshot(ts, rows, hmi, band):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    for r in rows:
        cur.execute(
            "INSERT INTO live_prices(ts, token, price, change24) VALUES (?,?,?,?)",
            (ts, r["token"], r["price"], r.get("change_24h"))
        )
        if r["btc_dom"] is not None:
            cur.execute(
                "INSERT INTO live_dom(ts, token, btc_dom, range_low, range_high) VALUES (?,?,?,?,?)",
                (ts, r["token"], r["btc_dom"], None, None)
            )

    if hmi is not None:
        cur.execute("INSERT INTO live_hmi(ts, hmi, band) VALUES (?,?,?)", (ts, hmi, band))

    con.commit()
    con.close()

async def writer_loop():
    ensure_dirs()
    db_init()

    while True:
        ts = now_iso()
        supplies = load_supplies()
        rows = compute_rows_and_dom(supplies)

        hmi, band = compute_hmi_from_live()

        # prices_latest.json (live)
        prices_payload = {
            "timestamp": ts,
            "rows": rows
        }
        write_json_atomic(os.path.join(LIVE_DIR, "prices_latest.json"), prices_payload)

        # hmi_latest.json (live)
        hmi_payload = {
            "timestamp": ts,
            "hmi": hmi if hmi is not None else None,
            "band": band
        }
        write_json_atomic(os.path.join(LIVE_DIR, "hmi_latest.json"), hmi_payload)

        # persist snapshot
        db_insert_snapshot(ts, rows, hmi, band)

        await asyncio.sleep(WRITE_EVERY_SECONDS)

async def main():
    await asyncio.gather(
        ws_spot_ticker(),
        writer_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())

