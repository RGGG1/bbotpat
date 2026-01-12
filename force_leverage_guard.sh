#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/force_leverage_guard_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
echo "---- START ----" | tee -a "$LOG"

# Do NOT use set -e (won't kill your terminal)
set -u
umask 022

echo "1) Stop executor (only)" | tee -a "$LOG"
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "2) Backup kc3_execute_futures.py" | tee -a "$LOG"
cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || {
  echo "Backup failed, aborting." | tee -a "$LOG"
  exit 1
}

echo "3) Patch kc3_execute_futures.py (force set+verify leverage before OPEN orders)" | tee -a "$LOG"
python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure imports
if not re.search(r'(?m)^\s*import\s+os\b', s):
    s = re.sub(r'(?m)^(import[^\n]*\n)', r'\1import os\n', s, count=1)

# Find the leverage callsite function (the one that POSTs /fapi/v1/leverage)
if "/fapi/v1/leverage" not in s:
    raise SystemExit("ABORT: kc3_execute_futures.py does not contain /fapi/v1/leverage")

# Insert helper block once near top
MARK = "# --- KC3_LEVERAGE_GUARD ---"
if MARK not in s:
    insert_at = 0
    m = re.search(r'(?m)^(import .+\n)+', s)
    if m:
        insert_at = m.end()
    helper = (
        "\n" + MARK + "\n"
        "def _kc3_clamp_int(x, lo, hi, default):\n"
        "    try:\n"
        "        v = int(float(x))\n"
        "    except Exception:\n"
        "        v = int(default)\n"
        "    if v < int(lo): v = int(lo)\n"
        "    if v > int(hi): v = int(hi)\n"
        "    return v\n\n"
        "def _kc3_env_lev_bounds():\n"
        "    lo = int(os.getenv('KC3_LEV_MIN', '5') or '5')\n"
        "    hi = int(os.getenv('KC3_LEV_MAX', '15') or '15')\n"
        "    base = int(os.getenv('KC3_LEV_BASE', os.getenv('KC3_LEVERAGE','10') or '10') or '10')\n"
        "    if lo > hi:\n"
        "        lo, hi = hi, lo\n"
        "    return lo, base, hi\n\n"
        "def _kc3_print(msg):\n"
        "    try:\n"
        "        print(msg, flush=True)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    s = s[:insert_at] + helper + "\n" + s[insert_at:]

# We need a function that can read leverage from positionRisk (verify)
# Find private_req(...) existence
if "def private_req" not in s:
    raise SystemExit("ABORT: Could not find def private_req in kc3_execute_futures.py")

# Add verifier function if missing
if "def _kc3_get_reported_leverage" not in s:
    # Put after helper mark
    idx = s.find(MARK)
    if idx < 0: idx = 0
    # place after helper block end: after _kc3_print definition
    m_end = re.search(r'(?s)'+re.escape(MARK)+r'.*?def _kc3_print\(msg\):.*?\n', s)
    ins = m_end.end() if m_end else idx
    verifier = (
        "\ndef _kc3_get_reported_leverage(symbol):\n"
        "    try:\n"
        "        data = private_req('GET', '/fapi/v2/positionRisk', {})\n"
        "        # data is a list of dicts\n"
        "        for r in (data or []):\n"
        "            if str(r.get('symbol','')) == str(symbol):\n"
        "                # leverage comes as string\n"
        "                return int(float(r.get('leverage', 0) or 0))\n"
        "    except Exception:\n"
        "        return None\n"
        "    return None\n"
    )
    s = s[:ins] + verifier + s[ins:]

# Patch the existing set_leverage behavior:
# locate the exact function containing POST /fapi/v1/leverage
m_set = re.search(r'(?s)(def\s+\w*leverage\w*\s*\(.*?\):\n)(.*?)(private_req\(\s*[\'"]POST[\'"]\s*,\s*[\'"]/fapi/v1/leverage[\'"]\s*,\s*\{.*?\}\s*\))', s)
if not m_set:
    raise SystemExit("ABORT: Could not find a leverage function with POST /fapi/v1/leverage")

fn_start = m_set.start(1)
fn_head  = m_set.group(1)
fn_body  = s[m_set.end(1):]  # everything after header

# We'll inject clamp+log+verify right before the leverage POST call inside that function.
# Find the leverage POST line within the function (first occurrence after header)
# We'll do a more direct replacement using the known string.
post_pat = r"private_req\(\s*[\'\"]POST[\'\"]\s*,\s*[\'\"]/fapi/v1/leverage[\'\"]\s*,\s*\{[^\}]*\}\s*\)"
m_post = re.search(post_pat, s[m_set.start():])
if not m_post:
    raise SystemExit("ABORT: Could not locate leverage POST call for injection")

# Determine symbol var name in that function signature (assume first arg named symbol)
# We'll just reference 'symbol' and 'lev' as used by existing code; if absent, we still clamp env fallback.
inject = (
    "    lo, base, hi = _kc3_env_lev_bounds()\n"
    "    lev = _kc3_clamp_int(locals().get('lev', base), lo, hi, base)\n"
    "    _kc3_print(f\"[{utc()}] [KC3] LEVERAGE_SET_REQUEST symbol={symbol} lev={lev} bounds={lo}-{hi}\")\n"
)

# Now rewrite the function: we’ll replace the first leverage POST call with:
# (inject) + (POST call using lev) + verify
orig_post = re.search(post_pat, s[m_set.start():]).group(0)

replacement_post = (
    inject +
    "    private_req(\"POST\", \"/fapi/v1/leverage\", {\"symbol\": symbol, \"leverage\": lev})\n"
    "    rep = _kc3_get_reported_leverage(symbol)\n"
    "    if rep is None:\n"
    "        _kc3_print(f\"[{utc()}] [KC3] LEVERAGE_VERIFY_FAIL symbol={symbol} lev={lev} reported=None\")\n"
    "        # do not proceed silently\n"
    "        raise RuntimeError(\"Could not verify leverage via positionRisk\")\n"
    "    _kc3_print(f\"[{utc()}] [KC3] LEVERAGE_VERIFIED symbol={symbol} requested={lev} reported={rep}\")\n"
    "    if int(rep) != int(lev):\n"
    "        raise RuntimeError(f\"Leverage mismatch requested={lev} reported={rep}\")\n"
)

s2 = s[m_set.start():]
s2 = s2.replace(orig_post, replacement_post, 1)
s = s[:m_set.start()] + s2

# Now we must ensure that OPEN orders always call leverage set right before placing order.
# Find the function that places /fapi/v1/order and inject set_leverage(symbol, lev) before it *only for opens*.
# We detect "reduceOnly": false as open path.
if "/fapi/v1/order" not in s:
    raise SystemExit("ABORT: No /fapi/v1/order in file")

# Inject guard before the order POST when reduceOnly is false.
# We'll patch the exact line: return private_req("POST", "/fapi/v1/order", params)
order_line_re = re.compile(r'(?m)^\s*return\s+private_req\(\s*[\'"]POST[\'"]\s*,\s*[\'"]/fapi/v1/order[\'"]\s*,\s*params\s*\)\s*$')
m_order = order_line_re.search(s)
if not m_order:
    raise SystemExit("ABORT: Could not find 'return private_req(\"POST\",\"/fapi/v1/order\", params)' line")

# Find nearby context: we need reduce_only variable name.
# We'll just look upward for 'reduce_only' in the previous ~40 lines.
start = max(0, m_order.start()-3000)
ctx = s[start:m_order.start()]
if "reduce_only" not in ctx:
    # still patch, but only if params has reduceOnly false (string)
    cond = "    if str(params.get('reduceOnly','false')).lower() == 'false':\n"
else:
    cond = "    if not reduce_only:\n"

# Determine lev variable: most code uses LEV or lev or env. We'll compute from env bounds and KC3_LEV_MODE if present.
guard = (
    "\n    # --- KC3 FORCE LEVERAGE ON OPEN ---\n"
    f"{cond}"
    "        lo, base, hi = _kc3_env_lev_bounds()\n"
    "        # prefer explicit lev if present, else base\n"
    "        lev_for_open = _kc3_clamp_int(locals().get('lev', base), lo, hi, base)\n"
    "        # call the leverage function (whatever it's named) by searching globals\n"
    "        # if 'set_leverage' exists use it; else try 'change_leverage'\n"
    "        if 'set_leverage' in globals():\n"
    "            set_leverage(symbol, lev_for_open)\n"
    "        elif 'change_leverage' in globals():\n"
    "            change_leverage(symbol, lev_for_open)\n"
    "        else:\n"
    "            # last resort: call private_req directly (still verified in patched function above if used)\n"
    "            private_req('POST','/fapi/v1/leverage',{'symbol': symbol, 'leverage': lev_for_open})\n"
    "        _kc3_print(f\"[{utc()}] [KC3] LEVERAGE_SET_BEFORE_OPEN symbol={symbol} lev={lev_for_open}\")\n"
)

# Insert guard just before the return line
s = s[:m_order.start()] + guard + s[m_order.start():]

p.write_text(s, encoding="utf-8")
print("OK: leverage is now forced+verified immediately before OPEN orders, and must log verification.")
PY

echo "4) Compile check" | tee -a "$LOG"
python3 -m py_compile kc3_execute_futures.py 2>>"$LOG" && echo "COMPILE OK" | tee -a "$LOG" || {
  echo "COMPILE FAILED. See $LOG" | tee -a "$LOG"
  exit 1
}

echo "5) Restart executor (robust wrapper)" | tee -a "$LOG"
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1
nohup python3 kc3_execute_futures_robust.py > kc3_exec.log 2>&1 &

sleep 2
echo "6) Executor running?" | tee -a "$LOG"
ps aux | grep kc3_execute_futures_robust.py | grep -v grep | tee -a "$LOG" || true

echo "---- DONE ----" | tee -a "$LOG"
echo "After NEXT OPEN, run: grep -n \"\\[KC3\\] LEVERAGE_\" kc3_exec.log | tail -n 80"
