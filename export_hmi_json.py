#!/usr/bin/env python3
"""
export_hmi_json.py

Lightweight exporter that reads the latest HMI row from
output/fg2_daily.csv and writes:

    hmi_latest.json
    docs/hmi_latest.json

This is useful if you recompute fg2_daily in batch and only want to
update the lightweight JSONs without hitting any external APIs.
"""

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

FG2_CSV = Path("output/fg2_daily.csv")

HMI_JSON_ROOT = Path("hmi_latest.json")
HMI_JSON_DOCS = Path("docs/hmi_latest.json")
Path("docs").mkdir(exist_ok=True)


def band_for_hmi(hmi: float) -> str:
    """
    Map HMI value (0–100) to a human-readable band label.

    Bands (per product spec):

        <15        -> "Zombie Apocalypse"
        15–30      -> "McDonald's Applications in high demand"
        30–40      -> "NGMI"
        40–60      -> "Stabled"
        60–80      -> "We're early"
        >=80       -> "It's the future of finance"
    """
    if hmi < 15:
        return "Zombie Apocalypse"
    if hmi < 30:
        return "McDonald's Applications in high demand"
    if hmi < 40:
        return "NGMI"
    if hmi < 60:
        return "Stabled"
    if hmi < 80:
        return "We're early"
    return "It's the future of finance"


def main():
    if not FG2_CSV.exists():
        raise SystemExit(f"{FG2_CSV} not found. Run compute_fg2_index.py first.")

    df = pd.read_csv(FG2_CSV, parse_dates=["date"])
    if df.empty:
        raise SystemExit(f"{FG2_CSV} is empty; nothing to export.")

    df = df.sort_values("date")
    last = df.iloc[-1]

    try:
        hmi_val = float(last["FG_lite"])
    except Exception:
        raise SystemExit("FG_lite column missing or invalid in fg2_daily.csv")

    band = band_for_hmi(hmi_val)
    date_val = last["date"]

    payload = {
        "hmi": round(hmi_val, 1),
        "band": band,
        "date": str(date_val.date() if hasattr(date_val, "date") else date_val),
        "exported_at": datetime.utcnow().isoformat() + "Z",
    }

    HMI_JSON_ROOT.write_text(json.dumps(payload, indent=2))
    HMI_JSON_DOCS.write_text(json.dumps(payload, indent=2))
    print(f"Wrote HMI JSONs: {HMI_JSON_ROOT}, {HMI_JSON_DOCS} (HMI={payload['hmi']}, band={band})")


if __name__ == "__main__":
    main()
