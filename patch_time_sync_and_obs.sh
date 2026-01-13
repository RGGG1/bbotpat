#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/patch_time_sync_and_obs_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  set -u
  umask 022

  echo "=== 0) Backups ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch kc3_execute_futures.py (server-time offset + -1021 retry + leverage logs into log()) ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure imports
if not re.search(r'(?m)^\s*import\s+time\s*$', s):
    m = re.search(r'(?m)^(import|from)\s+.+\n', s)
    if m: s = s[:m.end()] + "import time\n" + s[m.end():]
    else: s = "import time\n" + s

# Ensure utc() exists; if not, add minimal
if "def utc(" not in s:
    # place after imports
    m = re.search(r'(?m)^(import|from)\s+.+\n(\s*(import|from)\s+.+\n)*', s)
    ins = m.end() if m else 0
    s = s[:ins] + "\nfrom datetime import datetime, timezone\n\ndef utc():\n    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')\n\n" + s[ins:]

# Ensure log() prints to stdout with timestamp
# If log() exists, leave it; otherwise create simple one.
if not re.search(r'(?m)^\s*def\s+log\(', s):
    s = s.replace("\n\ndef utc():", "\n\ndef log(msg: str):\n    print(f\"[{utc()}] {msg}\", flush=True)\n\n\ndef utc():")

# Inject time sync helper (idempotent)
if "# --- KC3_TIME_SYNC ---" not in s:
    insert_at = s.find("def private_req(")
    if insert_at == -1:
        raise SystemExit("Could not find def private_req(")
    helper = """
# --- KC3_TIME_SYNC ---
_KC3_TIME_OFFSET_MS = None
_KC3_TIME_OFFSET_TS = 0.0

def _kc3_now_ms() -> int:
    return int(time.time() * 1000)

def _kc3_refresh_time_offset_ms(force: bool = False) -> int:
    global _KC3_TIME_OFFSET_MS, _KC3_TIME_OFFSET_TS
    # refresh at most every 60s unless forced
    if (not force) and (_KC3_TIME_OFFSET_MS is not None) and (time.time() - _KC3_TIME_OFFSET_TS < 60):
        return int(_KC3_TIME_OFFSET_MS)
    try:
        d = requests.get(BASE_URL + "/fapi/v1/time", timeout=5).json()
        server = int(d.get("serverTime"))
        local = _kc3_now_ms()
        _KC3_TIME_OFFSET_MS = server - local
        _KC3_TIME_OFFSET_TS = time.time()
        return int(_KC3_TIME_OFFSET_MS)
    except Exception as e:
        # keep old offset if any
        if _KC3_TIME_OFFSET_MS is None:
            _KC3_TIME_OFFSET_MS = 0
        return int(_KC3_TIME_OFFSET_MS)

def _kc3_signed_timestamp_ms() -> int:
    off = _kc3_refresh_time_offset_ms(force=False)
    return _kc3_now_ms() + int(off)

def _kc3_is_timestamp_error(msg: str) -> bool:
    return ("\\\"code\\\":-1021" in msg) or ("'code': -1021" in msg) or ("Timestamp for this request is outside of the recvWindow" in msg)
"""
    s = s[:insert_at] + helper + "\n" + s[insert_at:]

# Patch private_req to use server-time timestamp and retry once on -1021
m = re.search(r'(?ms)^def private_req\(.*?\n(    .+\n)+', s)
if not m:
    raise SystemExit("Could not locate private_req block")

block = m.group(0)

# ensure recvWindow used; default 10000
if "recvWindow" not in block:
    # find where params is built
    # simplest: inject timestamp/recvWindow just before signing
    # locate line that sets params["timestamp"] or params.update(...)
    pass

# Replace timestamp assignment if present
block2 = block
# common patterns
block2 = re.sub(r'(?m)^\s*params\["timestamp"\]\s*=\s*.*$',
                '    params["timestamp"] = _kc3_signed_timestamp_ms()',
                block2)
block2 = re.sub(r'(?m)^\s*params\.update\(\{\s*"timestamp"\s*:\s*.*\}\)\s*$',
                '    params.update({"timestamp": _kc3_signed_timestamp_ms()})',
                block2)

# If still no timestamp injection, add near signature creation:
if "_kc3_signed_timestamp_ms" not in block2:
    # try inject after "params = params or {}" style
    block2 = re.sub(r'(?m)^(\s*params\s*=\s*params\s*or\s*\{\}\s*)$',
                    r'\1\n    params["timestamp"] = _kc3_signed_timestamp_ms()',
                    block2)

# Add recvWindow if not present (env KC3_RECV_WINDOW_MS default 10000)
if "recvWindow" not in block2:
    block2 = re.sub(r'(?m)^\s*params\["timestamp"\]\s*=\s*_kc3_signed_timestamp_ms\(\)\s*$',
                    '    params["timestamp"] = _kc3_signed_timestamp_ms()\n    params["recvWindow"] = int(float(os.getenv("KC3_RECV_WINDOW_MS","10000") or "10000"))',
                    block2)

# Wrap request failure to retry once on -1021
if "KC3_TS_RETRY" not in block2:
    # find the line that raises RuntimeError(f"...{r.text}")
    # We'll replace with logic.
    block2 = re.sub(
        r'(?m)^\s*raise RuntimeError\(f"\{method\} \{path\} failed \{r\.status_code\}: \{r\.text\}"\)\s*$',
        '    msg = f"{method} {path} failed {r.status_code}: {r.text}"\n'
        '    if _kc3_is_timestamp_error(msg):\n'
        '        # --- KC3_TS_RETRY ---\n'
        '        _kc3_refresh_time_offset_ms(force=True)\n'
        '        params["timestamp"] = _kc3_signed_timestamp_ms()\n'
        '        params["recvWindow"] = int(float(os.getenv("KC3_RECV_WINDOW_MS","10000") or "10000"))\n'
        '        # one retry\n'
        '        r2 = requests.request(method, BASE_URL + path, params=params if method=="GET" else None,\n'
        '                             data=None if method=="GET" else params, headers=headers, timeout=10)\n'
        '        if r2.status_code >= 400:\n'
        '            raise RuntimeError(f"{method} {path} failed {r2.status_code}: {r2.text}")\n'
        '        return r2.json()\n'
        '    raise RuntimeError(msg)',
        block2
    )

# Replace in file
s = s[:m.start()] + block2 + s[m.end():]

# Force leverage decision logs through log() (so they hit kc3_exec.log)
s = s.replace('print("[KC3] LEVERAGE_VERIFY symbol=%s requested=%s binance=%s" % (symbol, req_lev, bn_lev), flush=True)',
              'log("[KC3] LEVERAGE_VERIFY symbol=%s requested=%s binance=%s" % (symbol, req_lev, bn_lev))')

# In open_position leverage decision section: ensure a log line exists containing used_for_size=
if "used_for_size=" not in s:
    # nothing to do; your file already has it in prints; but ensure it's logged
    pass
# Replace the "print([KC3] LEVERAGE ..." with log(...)
s = re.sub(r'(?m)^\s*print\("\[KC3\] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s"\s*%\s*\(.*\)\s*,\s*flush=True\)\s*$',
           '    log("[KC3] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s" % (symbol, os.getenv("KC3_LEV_MODE","fixed"), z_score, req_lev, bn_lev, used_for_size))',
           s)

# Ensure set_leverage logs in log()
s = re.sub(r'(?m)^\s*print\(f"\[KC3\] LEVERAGE_SET .*?\)\s*$',
           '    log(f"[KC3] LEVERAGE_SET {symbol} lev={lev}")',
           s)

p.write_text(s, encoding="utf-8")
print("OK: patched kc3_execute_futures.py (time sync + -1021 retry + leverage logs via log())")
PY

  echo "=== 2) Patch kc3_execute_futures_robust.py (TP_CHECK logging + guard timestamp error so loop doesn’t die) ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Ensure a lightweight file logger exists to kc3_exec.log
if "_kc3_filelog(" not in s:
    # insert near top after imports
    m = re.search(r'(?m)^(import|from)\s+.+\n(\s*(import|from)\s+.+\n)*', s)
    ins = m.end() if m else 0
    add = """
# --- KC3_FILELOG ---
def _kc3_filelog(msg: str):
    try:
        with open("kc3_exec.log","a",encoding="utf-8") as f:
            f.write(msg.rstrip()+"\\n")
    except Exception:
        pass
"""
    s = s[:ins] + add + s[ins:]

# Add TP_CHECK print if missing
if "TP_CHECK" not in s:
    # inject just before TP hit evaluation print line
    hit = re.search(r'(?m)^\s*print\(f"\[\{utc\(\)\}\] TP hit ', s)
    if hit:
        # find indentation level of that print
        line_start = s.rfind("\n", 0, hit.start()) + 1
        indent = re.match(r'\s*', s[line_start:hit.start()]).group(0)
        inject = indent + 'print(f"[{utc()}] TP_CHECK sym={sym} roi={roi:.4f} tp_thr={tp_thr:.4f} mode={tp_mode} vol={(tp_vol if tp_vol is not None else \'NA\')}", flush=True)\n'
        s = s[:line_start] + inject + s[line_start:]
        print("OK: injected TP_CHECK")
    else:
        print("WARN: could not find TP hit print to inject TP_CHECK; leaving as-is")

# Guard main loop against timestamp error (-1021) when calling current_roi/get_position
# Replace the specific crash site: "roi = current_roi(sym)" with try/except logging and continue
s = re.sub(
    r'(?m)^\s*roi\s*=\s*current_roi\(sym\)\s*$',
    '            try:\n'
    '                roi = current_roi(sym)\n'
    '            except Exception as e:\n'
    '                msg = str(e)\n'
    '                print(f"[{utc()}] WARN current_roi failed: {msg}", flush=True)\n'
    '                # timestamp drift or transient API errors should not kill the wrapper\n'
    '                _kc3_filelog(f"[{utc()}] WARN current_roi failed: {msg}")\n'
    '                time.sleep(2)\n'
    '                continue',
    s
)

p.write_text(s, encoding="utf-8")
print("OK: patched robust (TP_CHECK + guard current_roi)")
PY

  echo "=== 3) Compile checks ==="
  python3 -m py_compile kc3_execute_futures.py || exit 1
  python3 -m py_compile kc3_execute_futures_robust.py || exit 1
  echo "OK: compiles"

  echo "=== DONE (no restart yet) ==="
) 2>&1 | tee "$LOG"
