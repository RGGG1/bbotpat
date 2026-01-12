#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/fix_robust_margin_cooldown_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
echo "---- START ----" | tee -a "$LOG"
set -u
umask 022

echo "0) (No restart) Just patch + compile" | tee -a "$LOG"
echo "1) Backup robust executor" | tee -a "$LOG"
cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || { echo "Backup failed"; exit 1; }

echo "2) Patch robust: cooldown on -2019 (margin insufficient) in OUTER exception handler" | tee -a "$LOG"
python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure os is imported
if not re.search(r'(?m)^\s*import\s+os\s*$', s):
    m = re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m:
        s = s[:m.end()] + "import os\n" + s[m.end():]
    else:
        s = "import os\n" + s

# Ensure time is imported (it should be, but just in case)
if not re.search(r'(?m)^\s*import\s+time\s*$', s):
    m = re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m:
        s = s[:m.end()] + "import time\n" + s[m.end():]
    else:
        s = "import time\n" + s

MARK = "# --- KC3_MARGIN_COOLDOWN ---"
if MARK not in s:
    ins = s.find("def main(")
    if ins == -1:
        raise SystemExit("Could not find def main(")
    helper = (
        f"{MARK}\n"
        "def _kc3_is_margin_insufficient_msg(msg: str) -> bool:\n"
        "    if not msg:\n"
        "        return False\n"
        "    return ('\"code\":-2019' in msg) or (\"'code': -2019\" in msg) or ('Margin is insufficient' in msg)\n\n"
        "def _kc3_now_tag() -> str:\n"
        "    # Prefer existing utc() helper if present\n"
        "    try:\n"
        "        return utc()\n"
        "    except Exception:\n"
        "        return 'NOW'\n\n"
    )
    s = s[:ins] + helper + s[ins:]

# Find OUTER try/except in while True loop
m = re.search(r'(?s)while\s+True\s*:\s*\n\s*try\s*:\s*\n.*?\n(\s*)except\s+Exception\s+as\s+e\s*:\s*\n', s)
if not m:
    raise SystemExit("Could not find outer 'except Exception as e' in while True loop")

except_block_indent = m.group(1) + "    "  # inside except block
inject = (
    f"{except_block_indent}msg = str(e)\n"
    f"{except_block_indent}if _kc3_is_margin_insufficient_msg(msg):\n"
    f"{except_block_indent}    cd = float(os.getenv('KC3_MARGIN_COOLDOWN_SEC','120') or '120')\n"
    f"{except_block_indent}    state['cooldown'] = 'margin'\n"
    f"{except_block_indent}    state['cooldown_signal_id'] = state.get('open_signal_id')\n"
    f"{except_block_indent}    state['cooldown_until'] = time.time() + cd\n"
    f"{except_block_indent}    save_state(state)\n"
    f"{except_block_indent}    print(f\"[{{_kc3_now_tag()}}] KC3 MARGIN_INSUFFICIENT (-2019) -> cooldown {{int(cd)}}s\", flush=True)\n"
    f"{except_block_indent}    time.sleep(1)\n"
    f"{except_block_indent}    continue\n"
)

pos = m.end(0)
# Only inject once
if "KC3 MARGIN_INSUFFICIENT (-2019)" not in s[pos:pos+3000]:
    s = s[:pos] + inject + s[pos:]

p.write_text(s, encoding="utf-8")
print("OK: injected margin cooldown handler")
PY

echo "3) Compile check robust executor" | tee -a "$LOG"
python3 -m py_compile kc3_execute_futures_robust.py 2>>"$LOG" || { echo "COMPILE FAILED. See $LOG"; exit 1; }

echo "---- DONE ----" | tee -a "$LOG"
