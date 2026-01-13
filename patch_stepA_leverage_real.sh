#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/patch_stepA_leverage_real_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"
(
  set -u
  umask 022

  echo "=== 0) Backup kc3_execute_futures.py ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch kc3_execute_futures.py (real dynamic lev sizing + verify + clean logs) ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# --- helpers to inject (idempotent) ---
HELP = r"""
# --- KC3_LEVERAGE_RUNTIME_HELPERS ---
def _kc3_env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return default

def _kc3_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default

def _kc3_get_exchange_leverage(symbol: str):
    "Returns int leverage from /fapi/v2/positionRisk for symbol, or None."
    try:
        d = private_req("GET", "/fapi/v2/positionRisk", {})
        for x in d:
            if x.get("symbol") == symbol:
                return int(float(x.get("leverage", 0) or 0)) or None
    except Exception:
        return None
    return None

def _kc3_requested_leverage(z_score):
    "Computes requested leverage based on env knobs; returns int."
    mode = (os.getenv("KC3_LEV_MODE", "fixed") or "fixed").strip().lower()
    lev_fixed = _kc3_env_int("KC3_LEVERAGE", 10)
    lev_min   = _kc3_env_int("KC3_LEV_MIN", 5)
    lev_base  = _kc3_env_int("KC3_LEV_BASE", lev_fixed)
    lev_max   = _kc3_env_int("KC3_LEV_MAX", 15)
    z_full    = _kc3_env_float("KC3_LEV_Z_FULL", 2.6)

    if mode != "dynamic":
        return _kc3_clamp_lev(lev_fixed, lev_min, lev_max)

    try:
        z = abs(float(z_score))
    except Exception:
        z = 0.0

    # ramp from base -> max as |z| increases; at z_full we hit max
    if z_full <= 0:
        req = lev_base
    else:
        frac = min(1.0, z / z_full)
        req = int(round(lev_base + frac * (lev_max - lev_base)))

    return _kc3_clamp_lev(req, lev_min, lev_max)
"""

if "KC3_LEVERAGE_RUNTIME_HELPERS" not in s:
    # insert helpers after imports block
    m = re.search(r"(?ms)\A(.*?\n)(?=\n(def|class)\s)", s)
    if m:
        s = s[:m.end(1)] + "\n" + HELP.strip() + "\n\n" + s[m.end(1):]
    else:
        s = HELP.strip() + "\n\n" + s

# --- Clean up set_leverage: remove broken brace-print + duplicate clamp ---
# Remove the broken line: print(f"[KC3] LEVERAGE_SET symbol={{symbol}} lev={{lev}}", ...)
s = re.sub(r'(?m)^\s*print\(f"\[KC3\] LEVERAGE_SET symbol=\{\{symbol\}\} lev=\{\{lev\}\}"[^\n]*\)\s*\n', "", s)

# Remove duplicate clamp line if present (we keep one clamp with env min/max)
s = re.sub(r'(?m)^\s*lev\s*=\s*_kc3_clamp_lev\(lev\)\s*\n(?=\s*lev\s*=\s*_kc3_clamp_lev\(lev,\s*int\(os\.getenv)', "", s)

# Ensure set_leverage clamps with env bounds exactly once
pat_set = r"(?ms)def set_leverage\(symbol: str, lev: int\):\n(.*?)(?=\n\ndef |\nif __name__|\Z)"
m = re.search(pat_set, s)
if not m:
    raise SystemExit("Could not find set_leverage()")

block = m.group(0)
if "KC3_LEV_MIN" not in block or "KC3_LEV_MAX" not in block:
    # replace clamp section
    block2 = re.sub(
        r"(?m)^\s*lev\s*=\s*_kc3_clamp_lev\(lev.*\)\s*$",
        "    lev = _kc3_clamp_lev(lev, int(os.getenv('KC3_LEV_MIN','5') or '5'), int(os.getenv('KC3_LEV_MAX','15') or '15'))",
        block
    )
    block = block2

# Ensure it prints one clean LEVERAGE_SET line
if "LEVERAGE_SET" not in block:
    # add after leverage call
    block = re.sub(
        r'(?m)^\s*private_req\("POST",\s*"/fapi/v1/leverage".*\)\s*$',
        '    private_req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})\n    print(f"[KC3] LEVERAGE_SET {symbol} lev={lev}", flush=True)',
        block
    )
else:
    # ensure flush=True on the remaining line
    block = re.sub(
        r'(?m)^\s*print\(f"\[KC3\] LEVERAGE_SET \{symbol\} lev=\{lev\}"\)\s*$',
        '    print(f"[KC3] LEVERAGE_SET {symbol} lev={lev}", flush=True)',
        block
    )

s = s[:m.start()] + block + s[m.end():]

# --- Patch open_position sizing: use requested/verified leverage ---
# Replace line: notional = bal * LEV * MARGIN_FRAC
if "notional = bal * LEV * MARGIN_FRAC" not in s:
    # allow for different spacing
    if not re.search(r"notional\s*=\s*bal\s*\*\s*LEV\s*\*\s*MARGIN_FRAC", s):
        raise SystemExit("Could not find notional = bal * LEV * MARGIN_FRAC in open_position()")

# Inject leverage decision + verify immediately before notional calc
inject = r"""
    # --- KC3_LEVERAGE_DECISION (requested + verified + safe sizing) ---
    req_lev = _kc3_requested_leverage(z_score)
    try:
        set_leverage(symbol, req_lev)
    except Exception as e:
        print("[KC3] WARN set_leverage failed symbol=%s lev=%s err=%s" % (symbol, req_lev, e), flush=True)
    bn_lev = _kc3_get_exchange_leverage(symbol)
    print("[KC3] LEVERAGE_VERIFY symbol=%s requested=%s binance=%s" % (symbol, req_lev, bn_lev), flush=True)
    lev_for_size = req_lev
    if isinstance(bn_lev, int) and bn_lev > 0:
        lev_for_size = min(req_lev, bn_lev)
    print("[KC3] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s" %
          (symbol, os.getenv('KC3_LEV_MODE','fixed'), z_score, req_lev, bn_lev, lev_for_size), flush=True)
"""

# Find the notional line and inject above it within open_position
# We'll do a targeted replace of the first occurrence inside open_position
op_pat = r"(?ms)def open_position\(.*?\):\n(.*?)(\n\s*notional\s*=\s*bal\s*\*\s*LEV\s*\*\s*MARGIN_FRAC\s*\n)"
m = re.search(op_pat, s)
if not m:
    raise SystemExit("Could not locate open_position() notional computation")

before = m.group(0)
if "KC3_LEVERAGE_DECISION" not in before:
    new_block = before.replace(m.group(2), "\n" + inject.rstrip() + m.group(2))
else:
    new_block = before

# Now replace the notional formula to use lev_for_size
new_block = re.sub(
    r"(\n\s*)notional\s*=\s*bal\s*\*\s*LEV\s*\*\s*MARGIN_FRAC\s*\n",
    r"\1notional = bal * float(lev_for_size) * MARGIN_FRAC\n",
    new_block,
    count=1
)

s = s[:m.start()] + new_block + s[m.end():]

p.write_text(s, encoding="utf-8")
print("OK: open_position now sizes using requested/verified leverage (safe), and logs LEVERAGE/VERIFY/SET cleanly.")
PY

  echo "=== 2) Compile check ==="
  python3 -m py_compile kc3_execute_futures.py || exit 1
  echo "OK: kc3_execute_futures.py compiles"

  echo "=== DONE STEP A ==="
) 2>&1 | tee "$LOG"
