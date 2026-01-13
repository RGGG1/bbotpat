#!/usr/bin/env python3
import json, re, sys, statistics
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter

AUDIT=Path("/root/bbotpat_live/data/kc3_trade_audit.jsonl")

def parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z","+00:00"))
    except Exception:
        return None

def main():
    hours = 36
    if len(sys.argv) >= 2:
        try:
            hours = float(sys.argv[1])
        except Exception:
            pass

    if not AUDIT.exists():
        print("NO AUDIT FILE:", AUDIT)
        return 1

    now = datetime.now(timezone.utc)
    cut = now - timedelta(hours=hours)
    events=[]
    for line in AUDIT.read_text(errors="replace").splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            obj=json.loads(line)
        except Exception:
            continue
        ts=obj.get("ts")
        if not ts:
            continue
        t=parse_ts(ts)
        if not t:
            continue
        if t < cut:
            continue
        events.append(obj)

    print("CUT_UTC =", cut.isoformat().replace("+00:00","Z"))
    print("EVENTS_IN_WINDOW =", len(events))

    opens=[e for e in events if e.get("event")=="open"]
    closes=[e for e in events if e.get("event")=="close"]
    tp_hits=[e for e in events if e.get("event") in ("tp_hit","tp")]
    sl_events=[e for e in events if "sl" in str(e.get("event","")).lower() or "ladder" in str(e.get("event","")).lower()]

    print("opens =", len(opens), "closes =", len(closes), "tp_hits =", len(tp_hits), "sl_like_events =", len(sl_events))

    # leverage estimate distribution (notional/margin)
    levs=[]
    oob=0
    z_none=0
    z_vals=[]
    for e in opens:
        margin=e.get("margin")
        notional=e.get("notional")
        z=e.get("z")
        if z is None:
            z_none += 1
        else:
            z_vals.append(z)

        if isinstance(margin,(int,float)) and isinstance(notional,(int,float)) and margin>0:
            lev=notional/margin
            levs.append(lev)
            if lev < 4.9 or lev > 15.1:
                oob += 1

    print("z is None on", z_none, "of", len(opens), "opens")
    if z_vals:
        print("z sample (last 10):", z_vals[-10:])

    if levs:
        levs_sorted=sorted(levs)
        med=levs_sorted[len(levs_sorted)//2]
        print("lev_est MIN/MED/MAX =", round(levs_sorted[0],3), round(med,3), round(levs_sorted[-1],3))
        print("lev_est OUT_OF_BOUNDS =", oob)
    else:
        print("No margin/notional pairs found on opens.")

    # show last 15 opens in window
    print("\nLAST 15 OPENS:")
    for e in opens[-15:]:
        ts=e.get("ts")
        sym=e.get("symbol")
        side=e.get("side")
        z=e.get("z")
        margin=e.get("margin")
        notional=e.get("notional")
        lev_est=None
        if isinstance(margin,(int,float)) and isinstance(notional,(int,float)) and margin and margin>0:
            lev_est=notional/margin
        print(ts, sym, side, "z=", z, "lev_est=", (None if lev_est is None else round(lev_est,3)))

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
