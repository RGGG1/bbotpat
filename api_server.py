#!/usr/bin/env python3
"""
api_server.py

Phase 1: HMI API (no auth, no billing yet).

Endpoints:
  - GET /api/hmi/latest
  - GET /api/hmi/history?days=730

Data sources:
  - docs/hmi_latest.json
  - output/fg2_daily.csv (date, FG_lite, ...)
"""

from fastapi import FastAPI, HTTPException, Query
from pathlib import Path
from datetime import datetime
import csv
import json

app = FastAPI(title="HiveAI Oracle API", version="0.1.0")

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
OUTPUT = ROOT / "output"

HMI_LATEST_JSON = DOCS / "hmi_latest.json"
HMI_HISTORY_CSV = OUTPUT / "fg2_daily.csv"


def hmi_band_label(hmi: float) -> str:
    # Same semantics as front-end hmiBandLabel()
    if hmi < 10:
        return "zombie apocalypse"
    if hmi < 25:
        return "McDonald's applications"
    if hmi < 40:
        return "ngmi"
    if hmi < 60:
        return "stable"
    if hmi < 80:
        return "we're early"
    return "it's the future of finance"


@app.get("/api/hmi/latest")
def get_hmi_latest():
    """
    Latest HMI value and band.
    """
    if not HMI_LATEST_JSON.exists():
        raise HTTPException(status_code=503, detail="hmi_latest.json not found")

    try:
        raw = json.loads(HMI_LATEST_JSON.read_text())
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid hmi_latest.json")

    # Expected shape (your current format):
    # {
    #   "timestamp": "...",
    #   "hmi": 60.6,
    #   "hmi_band": "We're early",
    #   ...
    # }
    ts = raw.get("timestamp")
    hmi_raw = raw.get("hmi")

    try:
        hmi_val = float(hmi_raw)
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid 'hmi' in JSON")

    band = hmi_band_label(hmi_val)

    return {
        "timestamp": ts,
        "hmi": hmi_val,
        "band": band,
    }


@app.get("/api/hmi/history")
def get_hmi_history(days: int = Query(730, ge=1, le=3650)):
    """
    Historical daily HMI values based on output/fg2_daily.csv.

    Query params:
      - days: how many recent days to return (default 730, max 3650).
    """
    if not HMI_HISTORY_CSV.exists():
        raise HTTPException(status_code=503, detail="fg2_daily.csv not found")

    points = []

    try:
        with HMI_HISTORY_CSV.open("r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Expected columns: date, FG_lite, FG_vol, FG_oi, FG_spotperp, ...
                d = row.get("date")
                h_str = row.get("FG_lite")
                if not d or h_str is None:
                    continue
                try:
                    h_val = float(h_str)
                except Exception:
                    continue
                points.append(
                    {
                        "date": d,
                        "hmi": h_val,
                        "band": hmi_band_label(h_val),
                    }
                )
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read fg2_daily.csv")

    # Parse dates and sort ascending
    def parse_date(s: str):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    dated_points = [
        (parse_date(item["date"]), item)
        for item in points
        if parse_date(item["date"]) is not None
    ]
    dated_points.sort(key=lambda x: x[0])

    if not dated_points:
        return {"series": []}

    series_only = [item for _, item in dated_points][-days:]

    return {"series": series_only}
