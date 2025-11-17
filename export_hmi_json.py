#!/usr/bin/env python3
"""
export_hmi_json.py

Read the latest HMI value from output/fg2_daily.csv
and write it to:

- hmi_latest.json          (repo root, for scripts)
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


def band_for_hmi(h):
    if h < 10:
        return "Zombie apocalypse"
    if h < 25:
        return "McDonald's applications"
    if h < 40:
        return "Ngmi"
    if h < 60:
        return "Stable"
    if h < 80:
        return "We're early"
    return "It's the future of finance"


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

    payload = {
        "hmi": round(hmi, 1),
        "band": band_for_hmi(hmi),
        "date": date_str,
    }

    text = json.dumps(payload, indent=2)
    OUT_JSON_ROOT.write_text(text)
    OUT_JSON_DOCS.write_text(text)
    print(f"Wrote HMI JSONs: {OUT_JSON_ROOT}, {OUT_JSON_DOCS} (HMI={payload['hmi']})")


if __name__ == "__main__":
    main()
