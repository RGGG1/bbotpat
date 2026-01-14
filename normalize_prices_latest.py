#!/usr/bin/env python3
import json
from pathlib import Path

UNIVERSE_PATH = Path("data/kc3_token_universe.json")
PRICES_PATH   = Path("/var/www/bbotpat_live/prices_latest.json")

def main():
    uni = json.loads(UNIVERSE_PATH.read_text())
    data = json.loads(PRICES_PATH.read_text())

    rows = data.get("rows") or []
    by_tok = {str(r.get("token","")).upper(): r for r in rows if r.get("token")}

    out_rows = []
    missing = []

    for tok in uni:
        r = by_tok.get(tok)
        if r is None:
            missing.append(tok)
            # Placeholder row - makes missing tokens visible downstream
            r = {"token": tok, "price": None, "mc": None, "btc_dom": None, "range": None}
        out_rows.append(r)

    data["rows"] = out_rows
    PRICES_PATH.write_text(json.dumps(data, indent=2) + "\n")

    print("✅ normalized rows to universe size:", len(out_rows))
    if missing:
        print("⚠️ still missing data for:", missing)

if __name__ == "__main__":
    main()
