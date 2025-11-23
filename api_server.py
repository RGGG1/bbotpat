#!/usr/bin/env python3
"""
api_server.py

FastAPI app exposing:

- /api/hmi/latest
- /api/hmi/history
- /api/dom/options
- /api/dom/latest
- /api/dom/history

HMI is backed by:
- docs/hmi_latest.json (or hmi_latest.json)
- data/hmi_oi_history.csv

DOM is backed by:
- docs/prices_latest.json for live market caps
- docs/dom_mc_history.json for historical market caps per token
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"

# ------------------------------
# Shared helpers
# ------------------------------


def hmi_band_label(hmi: float) -> str:
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


# ------------------------------
# HMI models + loaders
# ------------------------------

class HMILatest(BaseModel):
    timestamp: Optional[str]
    hmi: float
    band: str


class HMIHistoryPoint(BaseModel):
    date: str
    hmi: float
    band: str


class HMIHistory(BaseModel):
    series: List[HMIHistoryPoint]


def load_hmi_latest() -> HMILatest:
    candidates = [
        DOCS / "hmi_latest.json",
        ROOT / "hmi_latest.json",
    ]
    data = None
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text())
            break
    if data is None:
        raise HTTPException(status_code=404, detail="hmi_latest.json not found")

    hmi_val = data.get("hmi")
    try:
        hmi = float(hmi_val)
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid HMI value in hmi_latest.json")

    ts = data.get("timestamp")  # may be None
    band = data.get("hmi_band") or hmi_band_label(hmi)

    return HMILatest(timestamp=ts, hmi=hmi, band=band)


def load_hmi_history(days: int) -> HMIHistory:
    path = DATA / "hmi_oi_history.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="hmi_oi_history.csv not found")

    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)

    series: List[HMIHistoryPoint] = []
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d_str = row.get("date")
            if not d_str:
                continue
            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            except Exception:
                continue
            if d_obj < start_date:
                continue
            try:
                hmi = float(row.get("FG_lite"))
            except Exception:
                continue
            series.append(
                HMIHistoryPoint(
                    date=d_str,
                    hmi=hmi,
                    band=hmi_band_label(hmi),
                )
            )

    return HMIHistory(series=series)


# ------------------------------
# DOM models + loaders
# ------------------------------

UNIVERSE_TOKENS = ["BTC", "ETH", "BNB", "SOL", "DOGE", "TON", "SUI", "UNI"]
MACROS = ["ALTS"]  # ALTS = all non-BTC


class DomOptions(BaseModel):
    tokens: List[str]
    macros: List[str]
    constraints: Dict[str, Any]


class DomLatestResponse(BaseModel):
    timestamp: Optional[str]
    x: List[str]
    y: List[str]
    dom: float
    mcX: float
    mcY: float
    range_min: Optional[float] = None
    range_max: Optional[float] = None
    range_days: Optional[int] = None


class DomHistoryPoint(BaseModel):
    date: str
    dom: float


class DomHistoryResponse(BaseModel):
    x: List[str]
    y: List[str]
    series: List[DomHistoryPoint]


def load_prices_latest() -> Tuple[Optional[str], Dict[str, float]]:
    """
    Returns (timestamp, token->mc) from prices_latest.json.
    """
    candidates = [
        DOCS / "prices_latest.json",
        ROOT / "prices_latest.json",
    ]
    data = None
    for path in candidates:
        if path.exists():
            data = json.loads(path.read_text())
            break
    if data is None:
        raise HTTPException(status_code=404, detail="prices_latest.json not found")

    ts = data.get("timestamp")
    rows = data.get("rows", [])
    mc_map: Dict[str, float] = {}
    for row in rows:
        token = row.get("token")
        if token not in UNIVERSE_TOKENS:
            continue
        mc_val = row.get("mc")
        try:
            mc = float(mc_val)
        except Exception:
            mc = 0.0
        mc_map[token] = mc
    return ts, mc_map


def load_dom_mc_history() -> Dict[str, Any]:
    """
    dom_mc_history.json structure:
    {
      "tokens": [...],
      "last_updated": "...",
      "series": [
        { "date": "YYYY-MM-DD", "mc": { "BTC": ..., "ETH": ..., ... } },
        ...
      ]
    }
    """
    candidates = [
        DOCS / "dom_mc_history.json",
        ROOT / "dom_mc_history.json",
    ]
    data = None
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = None
            break

    if data is None:
        raise HTTPException(status_code=404, detail="dom_mc_history.json not found")

    if "series" not in data or not isinstance(data["series"], list):
        raise HTTPException(status_code=500, detail="Invalid dom_mc_history.json structure")

    return data


def parse_dom_sides(
    pair: Optional[str],
    x_param: Optional[str],
    y_param: Optional[str],
) -> Tuple[List[str], List[str]]:
    """
    Parse pair/x/y query parameters into X and Y token lists.

    - pair=BTC_SOL  => X=['BTC'], Y=['SOL']
    - x=BTC&y=SOL,ETH
    - x=BTC&y=ALTS  (special macro)
    """
    if pair:
        if "_" not in pair:
            raise HTTPException(status_code=400, detail="pair must be like BTC_SOL")
        left, right = pair.split("_", 1)
        x_tokens = [left.strip().upper()] if left.strip() else []
        y_tokens = [right.strip().upper()] if right.strip() else []
    else:
        x_tokens = [t.strip().upper() for t in (x_param or "").split(",") if t.strip()]
        y_tokens = [t.strip().upper() for t in (y_param or "").split(",") if t.strip()]

    if not x_tokens or not y_tokens:
        raise HTTPException(status_code=400, detail="Must provide non-empty X and Y (via pair or x/y)")

    # Validate and handle ALTS macro
    def validate_side(tokens: List[str]) -> List[str]:
        out: List[str] = []
        for t in tokens:
            if t == "ALTS":
                out.append("ALTS")
            elif t in UNIVERSE_TOKENS:
                out.append(t)
            else:
                raise HTTPException(status_code=400, detail=f"Unknown token: {t}")
        return out

    x_tokens = validate_side(x_tokens)
    y_tokens = validate_side(y_tokens)

    # ALTS rule: only allowed on Y when X == ['BTC']
    if "ALTS" in x_tokens or "ALTS" in y_tokens:
        if x_tokens != ["BTC"] or y_tokens != ["ALTS"]:
            raise HTTPException(
                status_code=400,
                detail="ALTS macro currently only supported for X=BTC, Y=ALTS"
            )

    return x_tokens, y_tokens


def expand_tokens(tokens: List[str]) -> List[str]:
    """
    Expand macros like ALTS into concrete token lists.
    """
    if tokens == ["ALTS"]:
        return [t for t in UNIVERSE_TOKENS if t != "BTC"]
    out: List[str] = []
    for t in tokens:
        if t == "ALTS":
            out.extend([x for x in UNIVERSE_TOKENS if x != "BTC"])
        else:
            out.append(t)
    # Deduplicate but preserve order
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def compute_dom(mc_map: Dict[str, float], x_tokens: List[str], y_tokens: List[str]) -> Tuple[float, float, float]:
    """
    Given a single day's market caps and X/Y token lists (already expanded),
    compute (dom, mcX, mcY).
    """
    mcX = sum(mc_map.get(t, 0.0) for t in x_tokens)
    mcY = sum(mc_map.get(t, 0.0) for t in y_tokens)
    total = mcX + mcY
    if total <= 0:
        return 0.0, mcX, mcY
    dom = (mcX / total) * 100.0
    return dom, mcX, mcY


# ------------------------------
# FastAPI app + routes
# ------------------------------

app = FastAPI(
    title="HiveAI Oracle API",
    version="0.1.0",
)

# Open CORS for now (can tighten later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/hmi/latest", response_model=HMILatest)
def api_hmi_latest():
    return load_hmi_latest()


@app.get("/api/hmi/history", response_model=HMIHistory)
def api_hmi_history(days: int = Query(365, ge=1, le=730)):
    return load_hmi_history(days)


@app.get("/api/dom/options", response_model=DomOptions)
def api_dom_options():
    return DomOptions(
        tokens=UNIVERSE_TOKENS,
        macros=MACROS,
        constraints={
            "alts_only_with_btc": True,
            "max_tokens_per_side": len(UNIVERSE_TOKENS),
        },
    )


@app.get("/api/dom/latest", response_model=DomLatestResponse)
def api_dom_latest(
    pair: Optional[str] = Query(None),
    x: Optional[str] = Query(None),
    y: Optional[str] = Query(None),
):
    x_tokens_raw, y_tokens_raw = parse_dom_sides(pair, x, y)
    x_tokens = expand_tokens(x_tokens_raw)
    y_tokens = expand_tokens(y_tokens_raw)

    ts, mc_live = load_prices_latest()
    dom, mcX, mcY = compute_dom(mc_live, x_tokens, y_tokens)

    # Try to derive a range from history
    range_min = None
    range_max = None
    range_days = None
    try:
        hist = load_dom_mc_history()
        series = hist.get("series", [])
        dom_values: List[float] = []
        for entry in series:
            mc_map = entry.get("mc") or {}
            d_val, _, _ = compute_dom(mc_map, x_tokens, y_tokens)
            dom_values.append(d_val)
        if dom_values:
            range_min = min(dom_values)
            range_max = max(dom_values)
            range_days = len(dom_values)
    except HTTPException:
        # If no history yet, we still return live DOM
        pass

    return DomLatestResponse(
        timestamp=ts,
        x=x_tokens,
        y=y_tokens,
        dom=dom,
        mcX=mcX,
        mcY=mcY,
        range_min=range_min,
        range_max=range_max,
        range_days=range_days,
    )


@app.get("/api/dom/history", response_model=DomHistoryResponse)
def api_dom_history(
    pair: Optional[str] = Query(None),
    x: Optional[str] = Query(None),
    y: Optional[str] = Query(None),
    days: int = Query(365, ge=1, le=730),
):
    x_tokens_raw, y_tokens_raw = parse_dom_sides(pair, x, y)
    x_tokens = expand_tokens(x_tokens_raw)
    y_tokens = expand_tokens(y_tokens_raw)

    hist = load_dom_mc_history()
    series_all = hist.get("series", [])

    cutoff_date = date.today() - timedelta(days=days - 1)

    out_series: List[DomHistoryPoint] = []
    for entry in series_all:
        d_str = entry.get("date")
        mc_map = entry.get("mc") or {}
        if not d_str:
            continue
        try:
            d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if d_obj < cutoff_date:
            continue
        dom_val, _, _ = compute_dom(mc_map, x_tokens, y_tokens)
        out_series.append(DomHistoryPoint(date=d_str, dom=dom_val))

    return DomHistoryResponse(
        x=x_tokens,
        y=y_tokens,
        series=out_series,
    )


@app.get("/api/health")
def api_health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}
