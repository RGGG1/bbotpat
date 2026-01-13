#!/usr/bin/env bash
cd /root/bbotpat_live || exit 1
LOG="/root/bbotpat_live/patch_obs_logs_$(date +%Y%m%d_%H%M%S).log"
echo "Logging to: $LOG"
echo "If anything scrolls: tail -n 200 $LOG"

(
  set -u
  umask 022

  echo "=== 0) Backups ==="
  cp -a kc3_execute_futures.py "kc3_execute_futures.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1
  cp -a kc3_execute_futures_robust.py "kc3_execute_futures_robust.py.bak.$(date +%Y%m%d_%H%M%S)" || exit 1

  echo "=== 1) Patch base executor: send leverage logs via log() ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Replace key leverage print statements with log() equivalents (keep prints if you want, but log is what we need)
repls = [
    (r'(?m)^\s*print\("\[KC3\] LEVERAGE_VERIFY symbol=%s requested=%s binance=%s" % \(symbol, req_lev, bn_lev\), flush=True\)\s*$',
     '    log("[KC3] LEVERAGE_VERIFY symbol=%s requested=%s binance=%s" % (symbol, req_lev, bn_lev))'),
    (r'(?m)^\s*print\("\[KC3\] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s" % \([^\n]+\), flush=True\)\s*$',
     None),  # we'll handle via a more general injection
]

# If we can’t confidently regex-replace the long LEVERAGE line, inject a log right after it.
if "used_for_size=" in s and "[KC3] LEVERAGE symbol=" in s and "KC3_LEVERAGE_DECISION" in s:
    # Find the leverage decision block and ensure a log(...) exists there
    if "log(\"[KC3] LEVERAGE symbol=" not in s:
        # Insert log right after the existing print line (wherever it is)
        s = re.sub(
            r'(?m)^(?P<indent>\s*)print\("\[KC3\] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s" % \((?P<body>[^)]*)\), flush=True\)\s*$',
            r'\g<indent>print("[KC3] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s" % (\g<body>), flush=True)\n'
            r'\g<indent>log("[KC3] LEVERAGE symbol=%s mode=%s z=%s requested=%s binance=%s used_for_size=%s" % (\g<body>))',
            s
        )

# Replace LEVERAGE_SET prints with log lines too (keep print if present)
s = re.sub(
    r'(?m)^\s*print\(f"\[KC3\] LEVERAGE_SET ([^"]+)"\, flush=True\)\s*$',
    r'    log(f"[KC3] LEVERAGE_SET \1")',
    s
)

# Also handle the other LEVERAGE_SET pattern you have
s = re.sub(
    r'(?m)^\s*print\(f"\[KC3\] LEVERAGE_SET \{symbol\} lev=\{lev\}"\, flush=True\)\s*$',
    r'    log(f"[KC3] LEVERAGE_SET {symbol} lev={lev}")',
    s
)

p.write_text(s, encoding="utf-8")
print("OK: base leverage logs now mirrored into log()")
PY

  echo "=== 2) Patch robust: send TP logs via append-to-file logger ==="
  python3 - <<'PY'
from pathlib import Path
import re

p = Path("kc3_execute_futures_robust.py")
s = p.read_text(encoding="utf-8", errors="replace").replace("\t","    ")

# Add a tiny file-logger helper if missing
if "def _kc3_filelog(" not in s:
    ins = (
        "\n# --- KC3_FILELOG_HELPER ---\n"
        "def _kc3_filelog(msg: str, path: str = \"kc3_exec.log\"):\n"
        "    try:\n"
        "        with open(path, \"a\", encoding=\"utf-8\") as f:\n"
        "            f.write(str(msg).rstrip(\"\\n\") + \"\\n\")\n"
        "    except Exception:\n"
        "        pass\n"
    )
    # put it after imports (best-effort)
    m = re.search(r'(?m)^(import .+\n)+', s)
    if m:
        s = s[:m.end()] + ins + s[m.end():]
    else:
        s = ins + s

# Mirror TP hit print into file log (so grep works)
s = re.sub(
    r'(?m)^(?P<indent>\s*)print\(f"\[\{utc\(\)\}\] TP hit (?P<rest>.*)\)\s*$',
    r'\g<indent>print(f"[{utc()}] TP hit \g<rest>)\n'
    r'\g<indent>_kc3_filelog(f"[{utc()}] TP hit \g<rest>)',
    s
)

# If TP_CHECK exists, mirror it too. If not present, do nothing here (you already injected TP_CHECK earlier).
s = re.sub(
    r'(?m)^(?P<indent>\s*)print\(f"\[\{utc\(\)\}\] TP_CHECK (?P<rest>.*)\)\s*$',
    r'\g<indent>print(f"[{utc()}] TP_CHECK \g<rest>)\n'
    r'\g<indent>_kc3_filelog(f"[{utc()}] TP_CHECK \g<rest>)',
    s
)

p.write_text(s, encoding="utf-8")
print("OK: robust TP logs now mirrored into kc3_exec.log via _kc3_filelog()")
PY

  echo "=== 3) Compile checks ==="
  python3 -m py_compile kc3_execute_futures.py || exit 1
  python3 -m py_compile kc3_execute_futures_robust.py || exit 1
  echo "OK: both compile"

  echo "=== DONE (no restart performed) ==="
) 2>&1 | tee "$LOG"
