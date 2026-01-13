#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/patch_margin_retry_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  set -u
  umask 022

  echo "=== 0) Backup kc3_execute_futures.py ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch: retry smaller qty on -2019 ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure helper exists
if "_kc3_is_margin_insufficient" not in s:
    insert_after = re.search(r'(?m)^def private_req\(', s)
    if not insert_after:
        raise SystemExit("Could not find def private_req(")
    i = insert_after.start()
    helper = (
        "\n# --- KC3_MARGIN_INSUFFICIENT_HELPER ---\n"
        "def _kc3_is_margin_insufficient(exc: Exception) -> bool:\n"
        "    msg = str(exc)\n"
        "    return ('\"code\":-2019' in msg) or (\"'code': -2019\" in msg) or ('Margin is insufficient' in msg)\n"
    )
    s = s[:i] + helper + s[i:]

# Patch open_position market order call with retry loop if not already patched
if "KC3_MARGIN_RETRY" not in s:
    # find the place_market(...) call inside open_position (the OPEN order)
    # we look for: place_market(symbol, order_side, qty, reduce_only=False)
    pat = r'(?m)^(?P<indent>\s*)place_market\(symbol,\s*order_side,\s*qty,\s*reduce_only=False\)\s*$'
    m = re.search(pat, s)
    if not m:
        raise SystemExit("Could not find place_market(symbol, order_side, qty, reduce_only=False) in open_position")
    indent = m.group("indent")
    repl = (
        f"{indent}# --- KC3_MARGIN_RETRY ---\n"
        f"{indent}for _attempt, _scale in enumerate((1.0, 0.85, 0.70, 0.55), start=1):\n"
        f"{indent}    _q = qty if _scale == 1.0 else floor_step(qty * _scale, rules['step'], rules['qtyPrec'])\n"
        f"{indent}    if _q <= 0:\n"
        f"{indent}        continue\n"
        f"{indent}    try:\n"
        f"{indent}        if _attempt > 1:\n"
        f"{indent}            log(f\"KC3 WARN margin retry attempt={_attempt} scale={_scale} qty={_q} sym={symbol}\")\n"
        f"{indent}        place_market(symbol, order_side, _q, reduce_only=False)\n"
        f"{indent}        qty = _q  # update for logging\n"
        f"{indent}        break\n"
        f"{indent}    except Exception as e:\n"
        f"{indent}        if _kc3_is_margin_insufficient(e) and _attempt < 4:\n"
        f"{indent}            continue\n"
        f"{indent}        raise\n"
    )
    s = re.sub(pat, repl, s, count=1)

p.write_text(s, encoding="utf-8")
print("OK: margin retry patch applied")
PY

  echo "=== 2) Compile check ==="
  python3 -m py_compile kc3_execute_futures.py || exit 1
  echo "OK: compiles"

  echo "=== DONE (no restart performed) ==="
) 2>&1 | tee "$LOG"
