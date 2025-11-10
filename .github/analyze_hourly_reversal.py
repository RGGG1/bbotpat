#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time, math, requests
from statistics import pstdev
from datetime import datetime, timezone, timedelta

# =================== Config ===================
SYMBOL       = "BTCUSDT"                                 # change to ETHUSDT / SOLUSDT to test others
START        = datetime(2023,1,1,tzinfo=timezone.utc)
END          = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=1)
FEE_BPS      = 10        # 0.10% taker per side
TIME_STOP_H  = 96        # cap holding window
MIN_COOL_H   = 24        # cooldown after any exit before a new entry
START_EQUITY = 100.0

# Parameter grid (kept compact & sane)
LOOKBACKS   = [72, 120, 168]           # hours for z of returns (~3/5/7 days)
Z_THRESHES  = [2.0, 2.25, 2.5, 2.75]   # |z| threshold
RSI_LEN     = [14, 21]
RSI_GATES   = [(30, 70), (25, 75)]
BB_LEN      = [48, 72]                 # Bollinger basis length
BB_DEV      = [2.0]                    # std devs
ATR_LEN     = [24, 48]                 # ATR hours
TP_ATR      = [1.0, 1.5, 2.0]          # take profit in ATRs
SL_ATR      = [0.8, 1.0, 1.2]          # stop in ATRs

# 200h slope gate: require flat-ish regime (abs % slope over window <= gate)
SLOPE_N_H    = 200
SLOPE_DELTAH = 24
SLOPE_GATE   = 0.02    # 2% over 24h vs 200h mean => skip if stronger trend

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
]
HEADERS = {"User-Agent":"hourly-reversal/1.1 (+bbot)"}

# =================== Data =====================
def binance_klines_1h(symbol, start_dt, end_dt):
    out=[]
    start_ms=int(start_dt.timestamp()*1000)
    hard_end_ms=int(end_dt.timestamp()*1000)
    last_err=None; backoff=0.25
    while True:
        params={"symbol":symbol,"interval":"1h","limit":1500,"startTime":start_ms}
        got=None
        for base in BASES:
            try:
                r=requests.get(f"{base}/api/v3/klines", params=params, headers=HEADERS, timeout=30)
                if r.status_code in (451,403): last_err=Exception(f"{r.status_code} {r.reason}"); continue
                r.raise_for_status()
                got=r.json(); break
            except Exception as e:
                last_err=e
        if got is None:
            time.sleep(backoff); backoff=min(2.0, backoff*1.7); continue
        if not got: break
        for k in got:
            ct=int(k[6])//1000
            if ct>hard_end_ms: break
            out.append({
                "t": datetime.utcfromtimestamp(ct).replace(tzinfo=timezone.utc),
                "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])
            })
        start_ms = int(got[-1][6])+1
        if len(got)<1500 or start_ms>hard_end_ms: break
    # dedupe by time
    dd={}
    for x in out: dd[x["t"]]=x
    arr=sorted(dd.values(), key=lambda x:x["t"])
    return [x for x in arr if start_dt<=x["t"]<=end_dt]

# ================= Indicators =================
def rsi(closes, n=14):
    rsis=[None]*len(closes)
    gains=[0.0]; losses=[0.0]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1]
        gains.append(max(d,0.0)); losses.append(max(-d,0.0))
    if len(closes)<=n: return rsis
    avg_gain = sum(gains[1:n+1])/n
    avg_loss = sum(losses[1:n+1])/n
    rs = (avg_gain/avg_loss) if avg_loss>0 else float('inf')
    rsis[n]= 100 - 100/(1+rs) if avg_loss>0 else 100.0
    for i in range(n+1,len(closes)):
        avg_gain = (avg_gain*(n-1) + gains[i])/n
        avg_loss = (avg_loss*(n-1) + losses[i])/n
        rs = (avg_gain/avg_loss) if avg_loss>0 else float('inf')
        rsis[i]= 100 - 100/(1+rs) if avg_loss>0 else 100.0
    return rsis

def bollinger(closes, n=20, dev=2.0):
    mu=[None]*len(closes); sd=[None]*len(closes)
    for i in range(len(closes)):
        if i+1<n: continue
        w=closes[i+1-n:i+1]
        m = sum(w)/n
        v = sum((x-m)**2 for x in w)/n
        mu[i]=m; sd[i]=math.sqrt(v)
    upper=[None if mu[i] is None else mu[i]+dev*sd[i] for i in range(len(closes))]
    lower=[None if mu[i] is None else mu[i]-dev*sd[i] for i in range(len(closes))]
    return mu,upper,lower

def atr(ohlc, n=14):
    trs=[0.0]
    for i in range(1,len(ohlc)):
        h,l,c_prev = ohlc[i]["h"], ohlc[i]["l"], ohlc[i-1]["c"]
        tr = max(h-l, abs(h-c_prev), abs(l-c_prev))
        trs.append(tr)
    if len(ohlc)<=n: return [None]*len(ohlc)
    atrs=[None]*len(ohlc)
    sm = sum(trs[1:n+1])
    atrs[n]= sm/n
    for i in range(n+1,len(ohlc)):
        atrs[i]= (atrs[i-1]*(n-1)+trs[i])/n
    return atrs

def hourly_returns(closes):
    return [None] + [closes[i]/closes[i-1]-1.0 for i in range(1,len(closes))]

def z_of_returns(ret, look):
    z=[None]*len(ret)
    for i in range(len(ret)):
        if i<look: continue
        w=[x for x in ret[i-look+1:i+1] if x is not None]
        if len(w)<look: continue
        mu = sum(w)/look
        sd = pstdev(w) if look>1 else 0.0
        if not sd: continue
        last = ret[i]
        z[i] = (last - mu)/sd
    return z

def sma(values, n):
    out=[None]*len(values)
    run=0.0
    for i,v in enumerate(values):
        if v is None: return out
        run += v
        if i>=n: run -= values[i-n]
        if i>=n-1: out[i]= run/n
    return out

# ================ Backtest ====================
def backtest(ohlc, params):
    lb, z_thr, rsiN, rsi_lo, rsi_hi, bbN, bbDev, atrN, tp_k, sl_k = params
    closes=[x["c"] for x in ohlc]
    rs = rsi(closes, rsiN)
    mu,bbU,bbL = bollinger(closes, bbN, bbDev)
    atrs = atr(ohlc, atrN)
    rets = hourly_returns(closes)
    zs   = z_of_returns(rets, lb)

    # slope filter precompute
    base = sma(closes, SLOPE_N_H)
    def flat_regime(i):
        if i is None or i < max(SLOPE_N_H, SLOPE_DELTAH): return False
        if base[i] is None or base[i-SLOPE_DELTAH] is None: return False
        pct = abs(base[i] - base[i-SLOPE_DELTAH]) / base[i-SLOPE_DELTAH]
        return pct <= SLOPE_GATE

    def trade_logic(i):
        """Return 'LONG' or 'SHORT' or None at hour i (close of bar i)."""
        if zs[i] is None or rs[i] is None or bbU[i] is None or atrs[i] is None:
            return None
        if not flat_regime(i):      # skip in strong trend regimes
            return None
        sig=None
        # contrarian: big +z -> SHORT; big -z -> LONG; gated by RSI & bands
        if zs[i] >= z_thr and rs[i] >= rsi_hi and closes[i] >= bbU[i]:
            sig="SHORT"
        elif -zs[i] >= z_thr and rs[i] <= rsi_lo and closes[i] <= bbL[i]:
            sig="LONG"
        return sig

    # two runs: 1× and 10×
    def run_with_leverage(lev):
        equity=START_EQUITY
        peak=equity; maxdd=0.0
        in_pos=False; side=None; entry=None; start_i=None
        tp=None; sl=None
        trades=[]
        fee = FEE_BPS/10000.0
        last_exit_i = -10**9  # far in past for cooldown logic

        for i in range(len(ohlc)):
            # exit checks (if in trade) on close of bar i
            if in_pos:
                px = closes[i]
                roi = (px/entry-1.0) if side=="LONG" else (entry/px-1.0)
                gross = lev*roi
                hit_tp = (roi >= tp)
                hit_sl = (roi <= -sl)
                timed  = (i - start_i) >= TIME_STOP_H

                if hit_tp or hit_sl or timed:
                    # fees: entry (already taken) + exit:
                    equity = equity*(1+gross) - equity*(1+gross)*fee
                    trades.append({
                        "entry_t": ohlc[start_i]["t"], "exit_t": ohlc[i]["t"],
                        "side":side, "lev":lev, "entry":entry, "exit":px,
                        "tp%":tp*100, "sl%":sl*100, "roi%":gross*100,
                        "after":equity, "reason":"tp" if hit_tp else ("sl" if hit_sl else "expiry")
                    })
                    in_pos=False; side=None; entry=None; start_i=None
                    last_exit_i = i
                    peak=max(peak,equity); maxdd=max(maxdd, (peak-equity)/peak)
                    # (fall through to allow same-bar new entry only if cooldown == 0)

            # entry check
            if not in_pos and (i - last_exit_i) >= MIN_COOL_H:
                sig = trade_logic(i)
                if sig:
                    entry = closes[i]
                    atr_at_entry = atrs[i]
                    if atr_at_entry is None or atr_at_entry<=0: continue
                    tp = (tp_k * atr_at_entry) / entry
                    sl = (sl_k * atr_at_entry) / entry
                    side=sig; in_pos=True; start_i=i
                    # entry fee
                    equity -= equity*fee
                    peak=max(peak,equity)

        final = equity
        return final, maxdd, trades

    final_1x, dd_1x, trades_1x = run_with_leverage(1)
    final_10x, dd_10x, trades_10x = run_with_leverage(10)
    return {
        "final_1x":final_1x, "dd_1x":dd_1x, "trades_1x":trades_1x,
        "final_10x":final_10x, "dd_10x":dd_10x, "trades_10x":trades_10x
    }

# --------- drawdown-penalized ranking ----------
def score(res):
    # Penalize drawdown more than linearly; keep simple & transparent.
    # score = Final(1x) / (1 + 4*MaxDD_1x)
    return res["final_1x"] / (1.0 + 4.0*max(0.0, res["dd_1x"]))

# ================ Runner ======================
def main():
    print(f"Downloading {SYMBOL} 1h…")
    ohlc = binance_klines_1h(SYMBOL, START, END)
    if len(ohlc) < 2000:
        raise RuntimeError(f"Only {len(ohlc)} hourly bars returned; expected many more.")

    closes=[x["c"] for x in ohlc]
    print(f"Bars: {len(ohlc)} | First: {ohlc[0]['t']}  Last: {ohlc[-1]['t']}  | First close: {closes[0]:.2f}")

    # grid search
    print("\nSearching parameter grid…")
    results=[]
    for lb in LOOKBACKS:
        for zt in Z_THRESHES:
            for rlen in RSI_LEN:
                for rlo,rhi in RSI_GATES:
                    for bbl in BB_LEN:
                        for bbdev in BB_DEV:
                            for alen in ATR_LEN:
                                for tp_k in TP_ATR:
                                    for sl_k in SL_ATR:
                                        params=(lb,zt,rlen,rlo,rhi,bbl,bbdev,alen,tp_k,sl_k)
                                        try:
                                            res = backtest(ohlc, params)
                                        except Exception:
                                            continue
                                        results.append((score(res), res, params))

    if not results:
        print("No results — something went wrong with the grid or data.")
        return

    # rank by penalized score
    results.sort(key=lambda x: x[0], reverse=True)
    top = results[:10]

    print("\n=== Top 10 configs (penalized by 4× MaxDD) ===")
    print("Rank  Score    Final(1×)  MaxDD(1×)   Final(10×)  MaxDD(10×)   Params")
    for i,(sc,res,params) in enumerate(top,1):
        print(f"{i:>4}  {sc:7.2f}  {res['final_1x']:>9.2f}   {res['dd_1x']*100:>8.2f}%   "
              f"{res['final_10x']:>10.2f}   {res['dd_10x']*100:>9.2f}%   {params}")

    # take best and print trades for both 1× and 10×
    best_sc, best_res, best_params = top[0]
    print("\nBest params:", best_params)
    print(f"Score: {best_sc:.2f} | Final(1×) ${best_res['final_1x']:.2f}  MaxDD(1×) {best_res['dd_1x']*100:.2f}%")
    print(f"                     Final(10×) ${best_res['final_10x']:.2f} MaxDD(10×) {best_res['dd_10x']*100:.2f}%")

    print("\n— Trades (1×) —")
    print(f"{'#':>3}  {'Side':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'TP%':>6}  {'SL%':>6}  {'ROI%':>7}  {'After$':>9}  {'Exit'}")
    for i,tr in enumerate(best_res["trades_1x"],1):
        print(f"{i:>3}  {tr['side']:<5}  {tr['entry_t'].strftime('%Y-%m-%d %H:%M'):16}  "
              f"{tr['exit_t'].strftime('%Y-%m-%d %H:%M'):16}  {tr['tp%']:6.2f}  {tr['sl%']:6.2f}  "
              f"{tr['roi%']:7.2f}  {tr['after']:9.2f}  {tr['reason']}")

    if best_res["trades_10x"]:
        print("\n— Trades (10×) —")
        print(f"{'#':>3}  {'Side':<5}  {'Entry UTC':<16}  {'Exit UTC':<16}  {'TP%':>6}  {'SL%':>6}  {'ROI%':>7}  {'After$':>9}  {'Exit'}")
        for i,tr in enumerate(best_res["trades_10x"],1):
            print(f"{i:>3}  {tr['side']:<5}  {tr['entry_t'].strftime('%Y-%m-%d %H:%M'):16}  "
                  f"{tr['exit_t'].strftime('%Y-%m-%d %H:%M'):16}  {tr['tp%']:6.2f}  {tr['sl%']:6.2f}  "
                  f"{tr['roi%']:7.2f}  {tr['after']:9.2f}  {tr['reason']}")
    print(f"\nSummary (1×): Final ${best_res['final_1x']:.2f} | MaxDD {best_res['dd_1x']*100:.2f}%")
    print(f"Summary (10×): Final ${best_res['final_10x']:.2f} | MaxDD {best_res['dd_10x']*100:.2f}%")

if __name__=="__main__":
    main()
