#!/usr/bin/env python3
import json, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict

AUDIT = Path("/root/bbotpat_live/data/kc3_trade_audit.jsonl")
JOURNAL = Path("/root/bbotpat_live/data/kc3_trade_journal.jsonl")  # optional if you used it

def parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z","+00:00"))
    except Exception:
        return None

def load_jsonl(path: Path, cut: datetime):
    out=[]
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            obj=json.loads(line)
        except Exception:
            continue
        ts=obj.get("ts")
        t=parse_ts(ts) if isinstance(ts,str) else None
        if not t or t < cut:
            continue
        out.append(obj)
    return out

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def median(vals):
    if not vals:
        return None
    v=sorted(vals)
    return v[len(v)//2]

def main():
    cut = datetime.now(timezone.utc) - timedelta(hours=12)

    audit = load_jsonl(AUDIT, cut)
    journal = load_jsonl(JOURNAL, cut)

    print("CUT_UTC =", cut.isoformat().replace("+00:00","Z"))
    print("AUDIT_FILE =", str(AUDIT), "exists=" + str(AUDIT.exists()), "events=", len(audit))
    print("JOURNAL_FILE =", str(JOURNAL), "exists=" + str(JOURNAL.exists()), "events=", len(journal))
    print()

    # Flexible event detection (because naming differs across your patches)
    opens=[]
    closes=[]
    tp_hits=[]
    sl_rungs=[]
    lev_logs=[]
    close_reasons=Counter()

    # Helper to find event name
    def ev(obj):
        for k in ("event","type","kind","name"):
            v=obj.get(k)
            if isinstance(v,str) and v:
                return v
        return ""

    # Scan audit
    for obj in audit:
        e = ev(obj).upper()
        if "OPEN" in e:
            opens.append(obj)
        if "CLOSE" in e:
            closes.append(obj)
            r = obj.get("reason") or obj.get("close_reason") or obj.get("why")
            if isinstance(r,str) and r:
                close_reasons[r] += 1
        if "TP" in e and ("HIT" in e or "TAKE" in e):
            tp_hits.append(obj)
        if "SL" in e and ("LADDER" in e or "RUNG" in e):
            sl_rungs.append(obj)
        if "LEVERAGE" in e:
            lev_logs.append(obj)

    # Scan journal too (if present)
    for obj in journal:
        e = ev(obj).upper()
        if e == "SL_LADDER":
            sl_rungs.append(obj)
        if "TP" in e:
            tp_hits.append(obj)
        if "OPEN" in e:
            opens.append(obj)
        if "CLOSE" in e:
            closes.append(obj)

    print("LAST_12H opens =", len(opens), "closes =", len(closes), "tp_hits =", len(tp_hits), "sl_rungs =", len(sl_rungs))
    if close_reasons:
        print("CLOSE_REASONS:", dict(close_reasons))
    else:
        print("CLOSE_REASONS: (none recorded in audit)")
    print()

    # Leverage estimates: try multiple fields
    levs=[]
    oob=0
    for o in opens:
        lev = None
        # direct logged leverage?
        for k in ("lev","leverage","lev_used","leverage_used","req_lev"):
            lev = safe_float(o.get(k))
            if lev is not None:
                break
        # estimate from notional/margin if present
        if lev is None:
            notional = safe_float(o.get("notional"))
            margin = safe_float(o.get("margin"))
            if notional is not None and margin is not None and margin > 0:
                lev = notional/margin
        if lev is None:
            continue
        levs.append(lev)
        if lev < 4.9 or lev > 15.1:
            oob += 1

    if levs:
        print("LEV count =", len(levs), "MIN/MED/MAX =", round(min(levs),3), round(median(levs),3), round(max(levs),3))
        print("LEV out_of_bounds (expect 5..15) =", oob)
    else:
        print("LEV: no usable leverage fields found in OPEN events (audit may not include margin/notional/lev).")
    print()

    # Show last few TP hits / SL rungs for proof
    def show_tail(label, arr, n=8):
        print(label)
        for obj in arr[-n:]:
            ts=obj.get("ts")
            sym=obj.get("symbol") or obj.get("sym")
            side=obj.get("side")
            roi=obj.get("roi")
            thr=obj.get("thr") or obj.get("tp_thr")
            rung=obj.get("rung")
            print(" ", ts, sym, side, "roi=", roi, "thr=", thr, "rung=", rung, "event=", ev(obj))
        if not arr:
            print("  (none)")
        print()

    show_tail("TP_HITS (tail)", tp_hits)
    show_tail("SL_RUNGS (tail)", sl_rungs)

    # If we *aren't seeing* TP/SL events in audit, also confirm via raw kc3_exec.log greps.
    print("NOTE: If TP_HITS/SL_RUNGS are empty here, we should verify in kc3_exec.log with grep (next command below).")

if __name__ == "__main__":
    main()
