#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/fix_leverage_hard_clamp_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
echo "---- START ----" | tee -a "$LOG"
set -u
umask 022

echo "1) Stop executor only" | tee -a "$LOG"
pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
sleep 1

echo "2) Backup kc3_execute_futures.py" | tee -a "$LOG"
cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || { echo "Backup failed"; exit 1; }

echo "3) Patch kc3_execute_futures.py: clamp leverage inside leverage endpoint call" | tee -a "$LOG"
python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure import os exists
if not re.search(r'(?m)^\s*import\s+os\s*$', s):
    m = re.search(r'(?m)^import\s+[^\n]+\n', s)
    if m:
        s = s[:m.end()] + "import os\n" + s[m.end():]
    else:
        s = "import os\n" + s

CLAMP_MARK = "# --- KC3_LEVERAGE_CLAMP ---"
if CLAMP_MARK not in s:
    # Insert clamp helper after imports block
    mimp = re.search(r'(?m)^(import|from)\s+.+\n(?:^(import|from)\s+.+\n)*', s)
    ins = mimp.end() if mimp else 0
    helper = (
        f"\n{CLAMP_MARK}\n"
        "def _kc3_clamp_lev(v, lo=5, hi=15):\n"
        "    try:\n"
        "        x = int(float(v))\n"
        "    except Exception:\n"
        "        x = 10\n"
        "    if x < lo: x = lo\n"
        "    if x > hi: x = hi\n"
        "    return x\n\n"
    )
    s = s[:ins] + helper + s[ins:]

# Patch the line that posts to /fapi/v1/leverage to clamp lev right before sending
# We handle both: private_req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})
pat = r'private_req\(\s*"POST"\s*,\s*"/fapi/v1/leverage"\s*,\s*\{\s*"symbol"\s*:\s*symbol\s*,\s*"leverage"\s*:\s*lev\s*\}\s*\)'
m = re.search(pat, s)
if not m:
    raise SystemExit("ABORT: could not find the exact leverage POST call to patch in kc3_execute_futures.py")

# Inject clamping and logging immediately before that call, but only once.
# Find the line start that contains the call
call_start = s.rfind("\n", 0, m.start()) + 1
indent = re.match(r'[ \t]*', s[call_start:]).group(0)

inject = (
    f"{indent}lev = _kc3_clamp_lev(lev, int(os.getenv('KC3_LEV_MIN','5') or '5'), int(os.getenv('KC3_LEV_MAX','15') or '15'))\n"
    f"{indent}print(f\"[KC3] LEVERAGE_SET symbol={{{{symbol}}}} lev={{{{lev}}}}\", flush=True)\n"
)

# Avoid double-inject if already present nearby
window = s[max(0, call_start-500):m.start()]
if "LEVERAGE_SET" not in window:
    s = s[:call_start] + inject + s[call_start:]

p.write_text(s, encoding="utf-8")
print("OK: leverage POST is now hard-clamped + logged")
PY

echo "4) Compile check" | tee -a "$LOG"
python3 -m py_compile kc3_execute_futures.py 2>>"$LOG" || { echo "COMPILE FAILED. See $LOG"; exit 1; }

echo "---- DONE ----" | tee -a "$LOG"
