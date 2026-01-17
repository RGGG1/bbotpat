#!/usr/bin/env python3
import os, json, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parent
OUT  = Path("/var/www/bbotpat_live/prices_latest.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def load_env_file():
    envp = ROOT / ".env"
    if not envp.exists():
        return
    for raw in envp.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        os.environ[k] = v

def parse_alt_list():
    # Prefer env, but if missing, hard-default to old+new (no SHIB here).
    alt = os.getenv("KC3_ALT_LIST", "").strip()
    if alt:
        alt = alt.replace(",", " ")
        alt = " ".join(alt.split())
        toks = [t.upper() for t in alt.split() if t.strip()]
    else:
        toks = [
            "UNI","SOL","ETH","BNB","DOGE","TON","SUI",
            "XRP","ADA","LINK","XMR","XLM","ZEC","LTC","AVAX","HYPE",
        ]
    # de-dupe preserve order
    seen=set()
    out=[]
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

def http_json(url, timeout=10):
    req = Request(url, headers={"User-Agent":"kc3-collector"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

SPOT_ALL = "https://api.binance.com/api/v3/ticker/price"
FUT_ALL  = "https://fapi.binance.com/fapi/v1/ticker/price"
POLL_SEC = float(os.getenv("LIVE_COLLECTOR_POLL_SEC","1.0"))

def main():
    load_env_file()  # CRITICAL: do not depend on shell
    alts = parse_alt_list()
    want = ["BTC"] + alts
    want_syms = {f"{t}USDT": t for t in want}

    print("COLLECTOR TOKENS:", want, flush=True)

    while True:
        try:
            # fetch all tickers once, filter locally => no 400s
            spot = http_json(SPOT_ALL, timeout=10)
            fut  = http_json(FUT_ALL, timeout=10)

            # build maps symbol->price (prefer futures, fallback spot)
            spot_map = {}
            for x in spot:
                s = x.get("symbol")
                p = x.get("price")
                if s and p:
                    spot_map[s] = float(p)

            fut_map = {}
            for x in fut:
                s = x.get("symbol")
                p = x.get("price")
                if s and p:
                    fut_map[s] = float(p)

            rows = []
            missing = []
            for sym, tok in want_syms.items():
                price = fut_map.get(sym)
                if price is None:
                    price = spot_map.get(sym)
                if price is None:
                    missing.append(tok)
                else:
                    rows.append({"token": tok, "price": price})

            doc = {"timestamp": utc(), "rows": rows, "missing": missing}
            tmp = OUT.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2, sort_keys=False), encoding="utf-8")
            tmp.replace(OUT)

        except Exception as e:
            # never crash the process
            print("COLLECTOR ERROR:", repr(e), flush=True)

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
