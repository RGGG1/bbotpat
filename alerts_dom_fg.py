#!/usr/bin/env python3
"""
Wrapper to run the Dom + FG daily pipeline:

1) compute_fg2_index.py   -> builds FG_lite history
2) backtest_dominance_rotation.py -> updates equity_curve_fg_dom.csv and target weights
3) send_fg_dom_signal_telegram.py -> sends Telegram status + trade signal
"""

import subprocess

def run(cmd):
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

def main():
    run(["python", "compute_fg2_index.py"])
    run(["python", "backtest_dominance_rotation.py"])
    run(["python", "send_fg_dom_signal_telegram.py"])

if __name__ == "__main__":
    main()
  
