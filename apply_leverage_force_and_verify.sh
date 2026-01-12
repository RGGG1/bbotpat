#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/apply_leverage_force_and_verify_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
echo "---- START ----" | tee -a "$LOG"
set -u
umask 022

echo "1) Stop executor only" | tee -a "$LOG"
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "2) Backup kc3_execute_futures.py" | tee -a "$LOG"
cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || { echo "Backup failed" | tee -a "$LOG"; exit 1; }

echo "3) Patch kc3_execute_futures.py: force leverage+verify before non-reduceOnly orders" | tee -a "$LOG"
python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# We need a helper to fetch current leverage from positionRisk (if not already present)
if "def _kc3_get_current_leverage" not in s:
    # Insert helper near set_leverage (right after it)
    m = re.search(r"(?m)^def\s+set_leverage\s*\(.*?\n(?:^[ ]+.*\n)+", s)
    if not m:
        raise SystemExit("Could not find set_leverage() to anchor helper insert")

    helper = (
        "\n"
        "def _kc3_get_current_leverage(symbol: str):\n"
        "    try:\n"
        "        d = private_req('GET', '/fapi/v2/positionRisk', {})\n"
        "        for x in d:\n"
        "            if x.get('symbol') == symbol:\n"
        "                try:\n"
        "                    return int(float(x.get('leverage', 0) or 0))\n"
        "                except Exception:\n"
        "                    return None\n"
        "    except Exception:\n"
        "        return None\n"
        "    return None\n"
    )
    s = s[:m.end()] + helper + s[m.end():]

# Patch place_market() to force leverage before placing an OPEN (reduceOnly false)
# Find place_market definition block and inject just before private_req("POST","/fapi/v1/order"...)
pm = re.search(r"(?m)^def\s+place_market\s*\(.*\):\n", s)
if not pm:
    raise SystemExit("Could not find place_market()")
# locate the return private_req("POST", "/fapi/v1/order", params) inside place_market
ret = re.search(r"(?m)^\s*return\s+private_req\(\s*['\"]POST['\"]\s*,\s*['\"]/fapi/v1/order['\"]\s*,\s*params\s*\)\s*$", s)
if not ret:
    raise SystemExit("Could not find return private_req('POST','/fapi/v1/order',params)")

# We also need to know where params dict is created in place_market; we’ll insert a block
# immediately before the return that checks reduceOnly and forces leverage.
inject = (
    "    # KC3: force leverage setting immediately before OPEN orders\n"
    "    try:\n"
    "        ro = str(params.get('reduceOnly','false')).lower() == 'true'\n"
    "    except Exception:\n"
    "        ro = False\n"
    "    if not ro:\n"
    "        # requested leverage comes from dynamic/fixed logic stored in LEV (fallback)\n"
    "        req = None\n"
    "        try:\n"
    "            req = int(params.get('_kc3_req_lev')) if '_kc3_req_lev' in params else None\n"
    "        except Exception:\n"
    "            req = None\n"
    "        if req is None:\n"
    "            try:\n"
    "                req = int(float(os.getenv('KC3_LEVERAGE','10') or 10))\n"
    "            except Exception:\n"
    "                req = 10\n"
    "        # clamp using env min/max\n"
    "        try:\n"
    "            lo = int(float(os.getenv('KC3_LEV_MIN','5') or 5))\n"
    "            hi = int(float(os.getenv('KC3_LEV_MAX','15') or 15))\n"
    "        except Exception:\n"
    "            lo, hi = 5, 15\n"
    "        req = _kc3_clamp_lev(req, lo, hi)\n"
    "        set_leverage(symbol, req)\n"
    "        bn = _kc3_get_current_leverage(symbol)\n"
    "        print(f\"[KC3] LEVERAGE_VERIFY {symbol} requested={req} binance={bn}\", flush=True)\n"
    "        if bn is not None and (bn < lo or bn > hi):\n"
    "            raise RuntimeError(f\"Binance leverage verify failed for {symbol}: {bn} not in [{lo},{hi}]\")\n"
)

# Insert inject block right before the return inside place_market
s = s[:ret.start()] + inject + s[ret.start():]

# Now ensure open_position passes requested leverage through params
# We will tag params with _kc3_req_lev when calling place_market from open_position
# Search for place_market(symbol, order_side, qty, reduce_only=False)
s2 = re.sub(
    r"place_market\(\s*symbol\s*,\s*order_side\s*,\s*qty\s*,\s*reduce_only\s*=\s*False\s*\)",
    "place_market(symbol, order_side, qty, reduce_only=False)",
    s
)

# But we need to ensure params has _kc3_req_lev. Easiest: when building params dict in place_market,
# add params['_kc3_req_lev']=LEV if LEV exists in scope (it doesn't). So instead:
# We'll set a global last requested leverage before open_position calls place_market.
# Find open_position and before place_market(...) set global _KC3_LAST_REQ_LEV = req_lev.
if "_KC3_LAST_REQ_LEV" not in s2:
    s2 = " _KC3_LAST_REQ_LEV = None\n" + s2

# Add read of global into place_market params: when params dict created, set params['_kc3_req_lev']=_KC3_LAST_REQ_LEV
# Find params = { line in place_market
params_line = re.search(r"(?m)^\s*params\s*=\s*\{\s*$", s2)
if not params_line:
    raise SystemExit("Could not find params = { in place_market")

insert_after_params = "    try:\n        params['_kc3_req_lev'] = _KC3_LAST_REQ_LEV\n    except Exception:\n        pass\n"
# Insert right after the params dict is closed; detect the closing "}" of that dict by finding first line that starts with "}"
# after params_line.
lines = s2.splitlines(True)
i = 0
pos = 0
for idx, ln in enumerate(lines):
    if pos <= params_line.start() < pos + len(ln):
        i = idx
        break
    pos += len(ln)
# find closing brace line
j = None
for k in range(i, min(i+200, len(lines))):
    if re.match(r"^\s*\}\s*$", lines[k]):
        j = k
        break
if j is None:
    raise SystemExit("Could not find end of params dict in place_market")

# insert after closing brace line
lines.insert(j+1, insert_after_params)
s3 = "".join(lines)

# Patch open_position to set _KC3_LAST_REQ_LEV = req_lev right before set_leverage/place_market
op = re.search(r"(?m)^def\s+open_position\s*\(", s3)
if not op:
    raise SystemExit("Could not find open_position()")

# Find the first occurrence of set_leverage(symbol, req_lev) inside open_position and insert global assignment before it
mset = re.search(r"(?m)^\s*set_leverage\(\s*symbol\s*,\s*req_lev\s*\)\s*$", s3)
if not mset:
    raise SystemExit("Could not find set_leverage(symbol, req_lev) call to anchor _KC3_LAST_REQ_LEV")
s3 = s3[:mset.start()] + "    global _KC3_LAST_REQ_LEV\n    _KC3_LAST_REQ_LEV = req_lev\n" + s3[mset.start():]

p.write_text(s3, encoding="utf-8")
print("OK: patched to force+verify leverage immediately before OPEN orders")
PY

echo "4) Compile check" | tee -a "$LOG"
python3 -m py_compile kc3_execute_futures.py 2>>"$LOG" || { echo "COMPILE FAILED. See $LOG" | tee -a "$LOG"; exit 1; }

echo "---- DONE (not restarted) ----" | tee -a "$LOG"
