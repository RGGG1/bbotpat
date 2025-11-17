#!/usr/bin/env python3
"""
export_hmi_json.py

Read the latest HMI value from output/fg2_daily.csv
and write it to:

- hmi_latest.json          (repo root, for backwards compat)
- docs/hmi_latest.json     (for the website)

Expected CSV columns include:
- date
- FG_lite  (your HMI score)
"""

import json
from pathlib import Path

import pandas as pd

FG_CSV = Path("output/fg2_daily.csv")
OUT_JSON_ROOT = Path("hmi_latest.json")
OUT_JSON_DOCS = Path("docs/hmi_latest.json")


def main():
    if not FG_CSV.exists():
        raise SystemExit(f"CSV not found: {FG_CSV} (run compute_fg2_index.py first)")

    df = pd.read_csv(FG_CSV, parse_dates=["date"])
    if df.empty:
        raise SystemExit(f"CSV is empty: {FG_CSV}")

    df = df.sort_values("date")
    row = df.iloc[-1]

    hmi = float(row["FG_lite"])
    date_val = row["date"]

    try:
        date_str = pd.to_datetime(date_val).strftime("%Y-%m-%d")
    except Exception:
        date_str = str(date_val)

    # Simple band label – you can keep the same logic you used before
    if hmi < 40:
        band = "Ngmi"
    elif hmi < 60:
        band = "Stable"
    elif hmi < 80:
        band = "We're early"
    else:
        band = "Future of finance"

    payload = {
        "hmi": round(hmi, 1),
        "band": band,
        "date": date_str,
    }

    text = json.dumps(payload, indent=2)
    OUT_JSON_ROOT.write_text(text)
    OUT_JSON_DOCS.write_text(text)
    print(f"Wrote {OUT_JSON_ROOT} and {OUT_JSON_DOCS} with HMI={payload['hmi']} on {payload['date']}")


if __name__ == "__main__":
    main()
