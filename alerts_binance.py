#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts_binance.py
v3.0 – Binance adaptive signals + Telegram dashboard + live MFE/TP/SL tracking

Changes vs v2.0:
- Pulls full OHLC daily candles.
- Tracks a single active trade across days.
- Computes MFE using highs/lows since entry.
- Exits trade on TP/SL touch or at end of HOLD_BARS (time exit).
- Persists outcome, PnL, and MFE to state -> adaptive TP uses median MFE from history (>=5).
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta, date

# ────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────
COINS = [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH"), ("SOLUSDT", "SOL")]
Z_THRESH = 2.5                # z-score threshold for signal
SL = 0.03                     # stop loss (3%)
HOLD_BARS = 4                 # 96h = 4 daily candles
STATE_FILE = "adaptive_alerts_state.json"

# Fallback TPs (used until >=5 MFEs exist)
TP_FALLBACK = {"BTC": 0.0227, "ETH": 0.0167, "SOL": 0.0444}

# Telegram setup
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID")

BASES = [
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api
    
