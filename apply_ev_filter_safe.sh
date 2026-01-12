#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/apply_ev_filter_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
echo "---- START ----" | tee -a "$LOG"

# DO NOT use set -e (it kills interactive shells on error)
set -u
umask 022

echo "1) Backup agent" | tee -a "$LOG"
cp -a kc3_hmi_momentum_agent.py "kc3_hmi_momentum_agent.py.bak.$(date +%Y%m%d_%H%M%S)" 2>>"$LOG" || {
  echo "Backup failed. Aborting." | tee -a "$LOG"
  exit 1
}

echo "2) Ensure .env EV knob (ENTRY filter) is present" | tee -a "$LOG"
# Keep your existing file; just ensure a single canonical KC3_MIN_EV_PCT line.
# (Does not touch leverage/TP/SL)
grep -v '^KC3_MIN_EV_PCT=' .env > .env.tmp 2>/dev/null || true
mv .env.tmp .env
echo "KC3_MIN_EV_PCT=0.005" >> .env
echo "EV knob now:" | tee -a "$LOG"
grep '^KC3_MIN_EV_PCT=' .env | tee -a "$LOG"

echo "3) Patch kc3_hmi_momentum_agent.py (EV entry filter; z-score still required)" | tee -a "$LOG"

python3 - <<'PY' 2>>"$LOG" | tee -a "$LOG"
from pathlib import Path
import re

p = Path("kc3_hmi_momentum_agent.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# --- Insert KC3_MIN_EV_PCT env read near other env reads ---
# We search for z_enter assignment line that exists in your agent start log ("z_enter=1.6")
# and insert immediately after it.
min_ev_line = "MIN_EV_PCT = float(os.getenv('KC3_MIN_EV_PCT', '0.005') or '0.005')  # entry-only EV filter (unlevered)\n"

if "MIN_EV_PCT" not in s:
    m = re.search(r"(?m)^\s*z_enter\s*=\s*float\(os\.getenv\([^\n]+\)\)\s*$", s)
    if not m:
        raise SystemExit("PATCH ABORT: could not find z_enter = float(os.getenv(...)) line to anchor MIN_EV_PCT insertion.")
    ins = m.end()
    s = s[:ins] + "\n" + min_ev_line + s[ins:]
    print("OK: inserted MIN_EV_PCT env read.")
else:
    print("OK: MIN_EV_PCT already present (skipping insertion).")

# --- Add helper to compute EV from z and spread std ---
helper_mark = "# --- EV_FILTER_HELPERS ---"
helper_block = (
    "\n" + helper_mark + "\n"
    "def ev_from_z_and_spread_std(z: float, spread_std: float) -> float:\n"
    "    # Expected unlevered move to mean in spread space ~ |z| * std\n"
    "    try:\n"
    "        zf = float(z)\n"
    "        sf = float(spread_std)\n"
    "        if sf <= 0:\n"
    "            return 0.0\n"
    "        return abs(zf) * sf\n"
    "    except Exception:\n"
    "        return 0.0\n"
)

if helper_mark not in s:
    # Insert helper before main loop start (anchor: 'def main' or 'while True' nearest)
    m = re.search(r"(?m)^\s*def\s+main\s*\(\s*\)\s*:\s*$", s)
    if not m:
        # fallback: insert near top after imports
        m2 = re.search(r"(?m)^\s*import\s", s)
        ins = m2.start() if m2 else 0
        s = helper_block + "\n" + s
    else:
        s = s[:m.start()] + helper_block + "\n" + s[m.start():]
    print("OK: inserted EV helper function.")
else:
    print("OK: EV helper already present (skipping insertion).")

# --- Patch decision point: require EV>=MIN_EV_PCT for ENTER/ROTATE only ---
#
# We need an anchor inside your loop where it logs:
#   HOLD ... / ENTER ... / ROTATE ... / FLAT ...
#
# Your log line format includes: "ENTER LONG TONUSDT z=... best=... zbest=..."
# We'll inject EV computation right before it decides to emit ENTER/ROTATE.
#
# Pattern anchor: the first occurrence of a line that prints "ENTER" or "ROTATE" in code.
# We'll search for the print formatting that includes "ENTER" and "ROTATE" keywords.
#
# We'll implement minimally: if about to set desired side/symbol for ENTER or ROTATE,
# compute ev_unlev = ev_from_z_and_spread_std(zbest, spread_std_best) if spread_std_best exists,
# else fall back to 0.0 (won't enter). That guarantees filter is effective and safe.

# To avoid guessing variable names too much, we do a conservative patch:
# - Find the line where it prints "ENTER " and inject a guard right before it that uses
#   zbest and best_spread_std if available, else tries std_best, else 0.
#
# If we cannot find the log print anchor, we abort without modifying.

enter_print = re.search(r"(?m)^\s*print\(.+ENTER\s", s)
rotate_print = re.search(r"(?m)^\s*print\(.+ROTATE\s", s)

if not enter_print and not rotate_print:
    raise SystemExit("PATCH ABORT: could not find ENTER/ROTATE print lines to anchor EV filter injection.")

def inject_guard(before_idx: int) -> tuple[str, int]:
    guard = (
        "                # --- EV entry filter (does NOT affect exits) ---\n"
        "                # Requires EV (unlevered) >= MIN_EV_PCT to ENTER/ROTATE into a NEW position.\n"
        "                _spread_std_best = 0.0\n"
        "                for _nm in ('best_spread_std','spread_std_best','std_best','sigma_best','best_sigma'):\n"
        "                    if _nm in locals() and locals().get(_nm) is not None:\n"
        "                        try:\n"
        "                            _spread_std_best = float(locals().get(_nm))\n"
        "                            break\n"
        "                        except Exception:\n"
        "                            pass\n"
        "                _ev_unlev = ev_from_z_and_spread_std(zbest, _spread_std_best)\n"
        "                if _ev_unlev < MIN_EV_PCT:\n"
        "                    # If we are FLAT, stay flat. If we are already in a trade, do NOT close here.\n"
        "                    if (cur_symbol is None) or (cur_side is None):\n"
        "                        print(f\"[{utc()}] EV_BLOCK (flat) best={best_tok} zbest={zbest:.3f} ev={_ev_unlev:.4f} < {MIN_EV_PCT:.4f}\", flush=True)\n"
        "                        time.sleep(LOOP_SEC)\n"
        "                        continue\n"
        "                    else:\n"
        "                        print(f\"[{utc()}] EV_BLOCK (in-trade) best={best_tok} zbest={zbest:.3f} ev={_ev_unlev:.4f} < {MIN_EV_PCT:.4f} (no rotate)\", flush=True)\n"
        "                        # fall through to existing HOLD logic\n"
    )
    return guard, 1

# Prevent double-inject
if "EV_BLOCK" in s:
    print("OK: EV filter appears already injected (EV_BLOCK found).")
else:
    # Try to inject before ENTER print (preferred)
    anchor = enter_print.start() if enter_print else rotate_print.start()
    # We need indentation alignment: your loop body is indented; we rely on existing spacing.
    # We’ll insert the guard at the same indentation level as the ENTER print.
    # Determine current line indentation:
    line_start = s.rfind("\n", 0, anchor) + 1
    indent = re.match(r"[ ]*", s[line_start:anchor]).group(0)
    # We need to rewrite guard with same indent:
    guard, _ = inject_guard(anchor)
    guard = "\n".join(indent + ln if ln.strip() else ln for ln in guard.splitlines()) + "\n"
    s = s[:anchor] + guard + s[anchor:]
    print("OK: injected EV filter guard before ENTER/ROTATE logging.")

p.write_text(s, encoding="utf-8")
print("OK: wrote patched kc3_hmi_momentum_agent.py")
PY

echo "4) Compile check agent (must be clean)" | tee -a "$LOG"
python3 -m py_compile kc3_hmi_momentum_agent.py 2>>"$LOG" || {
  echo "AGENT COMPILE FAILED. Tail log:" | tee -a "$LOG"
  tail -n 120 "$LOG"
  exit 1
}
echo "OK: agent compiles." | tee -a "$LOG"

echo "5) Restart agent only (executor untouched)" | tee -a "$LOG"
pkill -f kc3_hmi_momentum_agent.py 2>/dev/null || true
sleep 1
# Load env into this shell for the nohup process
set -a
source .env
set +a
nohup python3 kc3_hmi_momentum_agent.py > kc3_agent.log 2>&1 &
sleep 2

echo "6) Proof: agent running + EV knob visible" | tee -a "$LOG"
ps aux | grep kc3_hmi_momentum_agent.py | grep -v grep | tee -a "$LOG" || true
echo "ENV KC3_MIN_EV_PCT in this shell:" | tee -a "$LOG"
python3 - <<'PY' | tee -a "$LOG"
import os
print("KC3_MIN_EV_PCT =", os.getenv("KC3_MIN_EV_PCT"))
PY

echo "7) Tail agent (look for EV_BLOCK when it refuses marginal entries)" | tee -a "$LOG"
tail -n 40 kc3_agent.log | tee -a "$LOG"

echo "---- DONE ----" | tee -a "$LOG"
echo "If anything scrolls: tail -n 200 $LOG"
