#!/usr/bin/env python3
"""股票筛选器 15min版 — docs/04-analysis/stock-screening.md"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path("data/minute_15")
W = {"sma": 26, "atr": 14, "bounce": 20, "z_thresh": -2.0}

def _s(v, t, lb):  # score [0,1]
    t1, t2, t3 = t
    if lb:
        if v <= t1: return 1.0
        if v <= t2: return 0.5 + 0.5 * (t2 - v) / (t2 - t1)
        if v <= t3: return 0.5 * (t3 - v) / (t3 - t2)
        return 0.0
    if v >= t1: return 1.0
    if v >= t2: return 0.5 + 0.5 * (v - t2) / (t1 - t2)
    if v >= t3: return 0.5 * (v - t3) / (t2 - t3)
    return 0.0

def _atr(df, p=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()

def score(sym, df):
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    c = df["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna()
    if len(r) < 80:
        return None

    # A. 均值回归性 (0.35)
    ac = r.autocorr(lag=1)
    sa = _s(ac, [-0.06, -0.02, 0.005], True)

    x = c.values; dx = np.diff(x); xl = x[:-1]
    m = ~np.isnan(xl) & ~np.isnan(dx)
    b = np.polyfit(xl[m], dx[m], 1)[0] if m.sum() > 20 else 0
    th = -b if b < 0 else 0.001
    hl = np.log(2) / th * 0.25  # bars→hours
    sh = _s(hl, [3, 8, 24], True)

    sma = c.rolling(W["sma"]).mean()
    std = c.rolling(W["sma"]).std()
    z = (c - sma) / std.replace(0, np.nan)
    rc = ev = 0
    for i in range(len(z) - W["bounce"]):
        if z.iloc[i] < W["z_thresh"] and not pd.isna(z.iloc[i]):
            ev += 1
            if (c.iloc[i+1:i+W["bounce"]+1] > sma.iloc[i]).any():
                rc += 1
    rp = rc / ev if ev > 0 else 0
    sr = _s(rp, [0.55, 0.35, 0.15], False)
    mr = (sa + sh + sr) / 3

    # B. 可交易性 (0.25) — 流动性 + 数据质量
    dv = c.mean() * df["volume"].mean()
    sd = _s(dv, [5e8, 1e8, 3e7], False)
    exp = len(pd.date_range(df.index.min(), df.index.max(), freq="15min"))
    cp = min(len(df) / max(exp, 1), 1.0)
    sc = _s(cp, [0.95, 0.85, 0.7], False)
    tr_score = (sd + sc) / 2

    # C. 风险 (0.15) — 只看回撤和缺口，波动率是利润来源不放这里
    peak = c.expanding().max()
    dd = abs((c - peak) / peak * 100).max()
    sdd = _s(dd, [12, 22, 38], True)
    do_ = df["open"].resample("D").first().dropna()
    dc_ = c.resample("D").last().shift(1).dropna()
    ci = do_.index.intersection(dc_.index)
    gr = (abs(do_.loc[ci] - dc_.loc[ci]) / dc_.loc[ci] > 0.02).sum() / max(len(ci), 1)
    sg = _s(gr, [0.03, 0.08, 0.18], True)
    # 趋势惩罚
    ur = (r > 0).sum() / len(r)
    ts = abs(ur - 0.5) * 2
    st = _s(ts, [0.12, 0.25, 0.45], True)
    risk = (sdd + sg + st) / 3

    # D. 交易机会 (0.20) — 信号密度 + 反弹幅度 + 波动率(利润空间)
    sf = (z < W["z_thresh"]).sum() / max(len(z.dropna()), 1) * 100
    ss = _s(sf, [5, 2, 0.5], False)
    bo = [];
    for i in range(len(z) - W["bounce"]):
        if z.iloc[i] < W["z_thresh"] and not pd.isna(z.iloc[i]):
            bo.append((c.iloc[i+W["bounce"]] - c.iloc[i]) / c.iloc[i] * 100)
    ab = np.mean(bo) if bo else 0
    sb = _s(ab, [1.0, 0.3, -0.1], False)
    ap = (_atr(df) / c * 100).mean()
    # 波动率: 0.6-2.5% 最优（有利润但可控）
    # 回测验证: <0.6% 利润空间不足(如NVDA 0.56%→亏损)
    if ap < 0.6: sv_ap = 0.0   # 硬门槛：太慢的不适合短线
    elif ap < 2.5: sv_ap = 1.0 - abs(ap-1.2)/1.3  # 越接近1.2%越好
    else: sv_ap = max(0, 1.0 - (ap-2.5)/2.5)      # >2.5% 逐步扣分
    opp = (ss + sb + sv_ap) / 3

    # 综合: 均值回归性主导(40%), 硬门槛惩罚低波动
    total = 0.40 * mr + 0.25 * tr_score + 0.15 * risk + 0.20 * opp
    # 额外惩罚: ATR<0.6% 直接扣 0.15 (NVDA 验证结论)
    if ap < 0.6: total -= 0.15

    return {"symbol": sym, "total": round(total, 3), "mr": round(mr, 3),
            "tr": round(tr_score, 3), "risk": round(risk, 3), "opp": round(opp, 3),
            "ac": round(ac, 3), "hl": round(hl, 1), "rp": round(rp, 2),
            "dv": round(dv/1e6, 0), "ap": round(ap, 2),
            "ts": round(ts, 2), "dd": round(dd, 1), "gr": round(gr, 2),
            "sf": round(sf, 2), "ab": round(ab, 2), "n": len(df)}

def tier(t): return "🟢A" if t>=0.70 else ("🟡B" if t>=0.55 else ("🟠C" if t>=0.40 else "🔴D"))

# main
files = sorted(DATA_DIR.glob("*_15m.parquet"))
results = [r for f in files if (r := score(f.stem.replace("_15m", ""), pd.read_parquet(f)))]

results.sort(key=lambda x: -x["total"])
print(f"{'股票':<6} {'总分':>5} {'档':>4} {'回归':>5} {'交易':>5} {'风险':>5} {'机会':>5} | {'自相关':>6} {'半衰h':>6} {'反弹':>5} {'成交M':>7} {'ATR%':>5} {'趋势':>5} {'回撤%':>6} {'缺口':>4} {'信号%':>5} {'弹幅':>5} {'K线':>5}")
print("─" * 120)
for r in results:
    print(f"{r['symbol']:<6} {r['total']:.3f} {tier(r['total']):>4} "
          f"{r['mr']:.3f} {r['tr']:.3f} {r['risk']:.3f} {r['opp']:.3f} | "
          f"{r['ac']:>6.3f} {r['hl']:>6.1f} {r['rp']:>5.2f} {r['dv']:>7.0f} "
          f"{r['ap']:>5.2f} {r['ts']:>5.2f} {r['dd']:>6.1f} {r['gr']:>4.2f} "
          f"{r['sf']:>5.2f} {r['ab']:>5.2f} {r['n']:>5}")

a = [r for r in results if r["total"]>=0.70]
b = [r for r in results if 0.55<=r["total"]<0.70]
c = [r for r in results if 0.40<=r["total"]<0.55]
print(f"\n🟢 A级({len(a)}): {[r['symbol'] for r in a]}")
print(f"🟡 B级({len(b)}): {[r['symbol'] for r in b]}")
print(f"🟠 C级({len(c)}): {[r['symbol'] for r in c]}")
