#!/usr/bin/env bash
set -u
cd /root/bbotpat_live || exit 1

LOG="/root/bbotpat_live/fix_robust_indent_216_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  umask 022

  echo "=== 0) Stop robust ==="
  pkill -f kc3_execute_futures_robust.py 2>/dev/null || true
  sleep 1
  pgrep -af kc3_execute_futures_robust.py || echo "robust stopped"

  echo "=== 1) Backup robust ==="
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 2) Show area around the crash (lines 190-250) ==="
  nl -ba kc3_execute_futures_robust.py | sed -n '190,250p'

  echo "=== 3) Patch: fix malformed if/try block near line ~216 ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# We expect the crash: "IndentationError: expected an indented block after 'if' statement on line 216"
# In your log, this is inside main() around roi = current_roi(sym).
# We'll do a targeted rewrite: replace the block that starts at:
#   if <something>:
#       try:
# with a safe, correctly-indented version that preserves behavior.

lines = s.splitlines(True)

# Find the first occurrence of "roi = current_roi(" inside main loop area
idx = None
for i, ln in enumerate(lines):
    if "roi = current_roi(" in ln:
        idx = i
        break

if idx is None:
    raise SystemExit("Could not find 'roi = current_roi(' in robust file; refusing to patch blindly.")

# Search upward for the nearest "if" line immediately above the roi line (within 20 lines)
if_i = None
for j in range(idx, max(idx-25, 0), -1):
    if re.match(r'^\s*if\s+.*:\s*$', lines[j]):
        if_i = j
        break

if if_i is None:
    raise SystemExit("Could not find enclosing 'if ...:' above roi line; refusing to patch blindly.")

# Determine indentation of that if
if_indent = re.match(r'^(\s*)', lines[if_i]).group(1)
block_indent = if_indent + "    "

# Now find end of this if-block by scanning until we hit a line with indentation <= if_indent (and not blank/comment)
end = None
for k in range(if_i+1, len(lines)):
    ln = lines[k]
    if ln.strip() == "" or re.match(r'^\s*#', ln):
        continue
    ind = re.match(r'^(\s*)', ln).group(1)
    if len(ind) <= len(if_indent):
        end = k
        break
if end is None:
    end = len(lines)

old_block = "".join(lines[if_i:end])

# Only patch if we see the suspicious "try:" near the start of the block OR if block is obviously malformed
if "try:" not in old_block and "current_roi(" not in old_block:
    raise SystemExit("Enclosing if-block doesn't look like ROI/try area; refusing to patch blindly.")

# Build a clean block:
# - keep the IF condition line as-is
# - inside it: wrap roi = current_roi(sym) in try/except and continue on failure
# This prevents loop death while still evaluating TP logic when roi is available.
if_line = lines[if_i].rstrip("\n")

new_block = ""
new_block += if_line + "\n"
new_block += block_indent + "try:\n"
new_block += block_indent + "    roi = current_roi(sym)\n"
new_block += block_indent + "except Exception as e:\n"
new_block += block_indent + "    print(f\"[{utc()}] WARN current_roi failed {sym} err={e}\", flush=True)\n"
new_block += block_indent + "    roi = None\n"

# We must preserve downstream logic: if later code expects roi to exist, it should be guarded.
# So if the old block had a 'roi =' already, we will not duplicate other lines;
# we’ll append any lines AFTER the roi assignment line that are still meaningful,
# but we will drop malformed try/except fragments.
# We'll keep lines from the old block that occur after the first occurrence of 'roi =' AND are indented deeper than the if.
keep = []
seen_roi = False
for ln in old_block.splitlines(True)[1:]:
    if not seen_roi:
        if "roi" in ln and "current_roi(" in ln:
            seen_roi = True
        continue
    # keep only lines that are inside this if-block (indented > if_indent)
    ind = re.match(r'^(\s*)', ln).group(1)
    if len(ind) <= len(if_indent):
        break
    # Drop raw 'try:' / 'except' that may be malformed
    if re.match(r'^\s*(try:|except\b|finally:)\s*$', ln):
        continue
    keep.append(ln)

# Now add guard to skip TP eval if roi is None
new_block += block_indent + "if roi is None:\n"
new_block += block_indent + "    continue\n"

# Append preserved remainder lines (if any)
new_block += "".join(keep)

# Replace in file
lines[if_i:end] = [new_block]
out = "".join(lines)
p.write_text(out, encoding="utf-8")
print("OK: patched ROI/try indentation block near line", if_i+1, "->", end)
PY

  echo "=== 4) Compile robust ==="
  python3 -m py_compile kc3_execute_futures_robust.py
  echo "OK: robust compiles"

  echo "=== 5) Start robust ==="
  set -a; source .env; set +a
  nohup python3 kc3_execute_futures_robust.py >> kc3_exec.log 2>&1 &
  sleep 2
  pgrep -af kc3_execute_futures_robust.py || echo "ROBUST NOT RUNNING"

  echo "=== 6) Tail last 60 ==="
  tail -n 60 kc3_exec.log
) 2>&1 | tee "$LOG"
