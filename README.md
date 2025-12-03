# HiveAI  
All signal, no noise.

HiveAI is an automated crypto intelligence and trading system built around four major components:

- **HMI (Hive Mind Index)**  
- **Dominance Oracle**  
- **Knifecatcher automated trading agents**  
- **Token Intelligence Table**

This project collects, processes, and publishes crypto market analytics, along with trading signals used by fully automated execution scripts and a live dashboard.

---

## 🧠 Hive Mind Index (HMI)

HMI is HiveAI’s custom-built Fear & Greed index, designed not just to measure sentiment but to identify **predictive turning points**.  
It considers:

- Open interest pressure  
- Perpetual futures basis (premium/discount)  
- Volatility environment  
- Market structure shifts  
- Behavioral expansion/compression patterns  

The index outputs:

- A 0–100 score  
- A sentiment “band” (e.g., Cautiously Bullish, Frothy, NGMI)  
- Visual color cues on the dashboard  

**Update frequency:** hourly  
**Output JSON:** `hmi_latest.json`

---

## 📈 Dominance Oracle

The Dominance Oracle measures the relative strength between BTC and any tracked asset or basket.  
It supports:

- BTC vs single asset  
- BTC vs ALTS basket  
- Token vs token  
- Multi-token dominance aggregation  
- Two-year dominance ranges  
- Real-time dominance and market-cap calculations  

Users can dynamically select X and Y assets to compare on the frontend.

**Update frequency:** hourly  
**Output JSON:**  
- `prices_latest.json`  
- `dom_history_latest.json`  
- `dom_bands_latest.json`

---

## 🤖 Knifecatcher (KC1 & KC2)

Knifecatcher agents are automated adaptive trading systems.

### KC1 — Rebalancer
KC1 is a volatility-responsive rebalancing system.  
It reports:

- Current portfolio value  
- ROI vs BTC buy-and-hold  
- Performance over time  

**Update frequency:** 2× daily  
**Output JSON:** `knifecatcher_latest.json`

### KC2 — Dominance + HMI Adaptive Rotator
KC2 is a fully automated, hourly-rotating trading model. It:

- Selects the strongest asset via dominance  
- Uses HMI to decide whether to risk-on or risk-off  
- Tracks entry and target prices for positions  
- Computes benchmark ROI vs BTC, ETH, BNB, SOL  
- Provides signals used by the execution engine  

**Update frequency:** hourly  
**Output JSON:** `dom_signals_hourly.json`

---

## 🪙 Token Intelligence Table

The dashboard also displays per-token intelligence, including:

- Current price  
- Market cap  
- Dominance vs BTC  
- Historical dominance range  
- Recommended action (ALT-heavy / BTC-heavy / Stables)  
- Potential ROI based on dominance normalization targets  

Currently tracked tokens include:

BTC, ETH, BNB, SOL, DOGE, TON, SUI, UNI.

USDTC is used to track the combined values, marketcaps of USDT and USDC, giving indicator of stable coin dominance.


More tokens available on demand.

---

# 🧩 System Architecture

\`\`\`
                           ┌───────────────────────┐
                           │  Global Market Data    │
                           │ (Prices, MC, Vol, OI)  │
                           └───────────┬────────────┘
                                       │
                     ┌─────────────────┼───────────────────┐
                     │                 │                   │
              ┌────────────┐   ┌──────────────┐   ┌────────────────┐
              │   HMI      │   │ Dominance     │   │ Knifecatcher   │
              │ (Fear/Greed│   │ Oracle Engine │   │ Automated Agents│
              └──────┬─────┘   └──────┬────────┘   └────────┬───────┘
                     │                │                     │
                     │      JSON      │       Signals       │
                     └─────────┬──────┴────────────┬────────┘
                                │                   │
                                ▼                   ▼
                       ┌─────────────────┐   ┌────────────────────┐
                       │ Frontend (docs) │   │ Trading Executor    │
                       │ Live Dashboard  │   │ + Notifications     │
                       └─────────────────┘   └────────────────────┘
