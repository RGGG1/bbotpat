#!/usr/bin/env python3
import json, time, csv
from pathlib import Path
from datetime import datetime, timezone

WEBROOT = Path("/var/www/bbotpat_live")
HMI_PATH = WEBROOT / "hmi_latest.json"
PRICES_PATH = WEBROOT / "prices_latest.json"
KC3_OUT = WEBROOT / "kc3_latest.json"
KC3_CSV = WEBROOT / "kc3_paper_trades.csv"

HMI_THRESHOLD = 0.1
EXCLUDE = {"BTC", "USDT", "USDC", "USDTC"}

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def atomic_write(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)

def load_last_trade_id():
    try:
        d = json.loads(KC3_OUT.read_text())
        return int(d.get("trade_id", 0))
    except Exception:
        return 0

class KC3PaperAgent:
    def __init__(self):
        self.prev_hmi = None
        self.anchor_hmi = None
        self.signal_side = "LONG"
        self.trade_id = load_last_trade_id()
        self.last_log = 0

    def best_token(self, prices):
        best_tok, best_pot = None, None
        for r in prices.get("rows", []):
            tok = str(r.get("token", "")).upper()
            if not tok or tok in EXCLUDE:
                continue
            pot = safe_float(r.get("pot_roi_pct"))
            if pot is None:
                frac = safe_float(r.get("pot_roi_frac"))
                if frac is None:
                    continue
                pot = frac * 100
            if best_pot is None or pot > best_pot:
                best_tok, best_pot = tok, pot
        return best_tok, best_pot

    def step(self):
        hmi_js = read_json(HMI_PATH)
        prices_js = read_json(PRICES_PATH)

        hmi = safe_float(hmi_js.get("hmi"))
        if hmi is None:
            return

        best_tok, best_pot = self.best_token(prices_js)
        hmi_delta = None if self.prev_hmi is None else round(hmi - self.prev_hmi, 6)

        if self.anchor_hmi is None:
            self.anchor_hmi = hmi
        else:
            move = hmi - self.anchor_hmi
            if abs(move) >= HMI_THRESHOLD:
                self.signal_side = "LONG" if move > 0 else "SHORT"
                self.anchor_hmi = hmi
                self.trade_id += 1
                print(f"[{utc_now_iso()}] FLIP -> {self.signal_side} move={move:.4f}", flush=True)

        out = {
            "timestamp": utc_now_iso(),
            "hmi": hmi,
            "hmi_delta": hmi_delta,
            "signal_side": self.signal_side,
            "best_token": best_tok,
            "best_pot_roi_pct": best_pot,
            "trade_id": self.trade_id,
        }

        atomic_write(KC3_OUT, out)

        new = not KC3_CSV.exists()
        with KC3_CSV.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(out.keys())
            w.writerow(out.values())

        self.prev_hmi = hmi
        if time.time() - self.last_log > 15:
            self.last_log = time.time()
            print(f"[{out['timestamp']}] hmi={hmi} side={self.signal_side} best={best_tok}", flush=True)

def main():
    agent = KC3PaperAgent()
    while True:
        agent.step()
        time.sleep(1)

if __name__ == "__main__":
    main()
