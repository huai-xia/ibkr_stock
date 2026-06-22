#!/usr/bin/env python3
"""
股票筛选器 (1min 版) — 基于 IBKR 7个月 1min K线
4维度评分: 均值回归性(40%) + 可交易性(25%) + 风险(15%) + 机会(20%)
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path("data/ibkr_1min")

# 5min 窗口参数 (78根/天, 与15min版共用阈值)
W = {"sma": 12, "atr": 14, "bounce": 12, "z_thresh": -2.0}
# sma=12×5min=1h, bounce=12×5min=1h


def _s(v, t, lb):
    """映射到 [0,1]"""
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


def score(sym, filepath, recent_days=60):
    df = pd.read_parquet(filepath)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # 取最近 N 个交易日 (默认60天≈3个月)
    all_dates = sorted(set(df.index.date))
    if len(all_dates) > recent_days:
        cutoff = all_dates[-recent_days]
        df = df[df.index.date >= cutoff]

    # 1min → 5min 重采样
    df5 = df.resample("5min").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum"
    }).dropna()
    if len(df5) < 300:
        return None

    df = df5
    c = df["close"].astype(float)
    r = np.log(c / c.shift(1)).dropna()
    if len(r) < 200:
        return None

    # ── A. 均值回归性 (0.40) ──
    # A1: 自相关 (1min 级别，负值更强)
    ac1 = r.autocorr(lag=1)
    ac5 = r.autocorr(lag=5)
    ac_combo = ac1 * 0.7 + ac5 * 0.3  # lag1+lag5 加权
    sa = _s(ac_combo, [-0.04, -0.015, 0.0], True)

    # A2: 去趋势回归速度 (用中位数回归时间替代OU半衰期)
    # 去趋势: 价格 - 慢速SMA(78期 = 1天)
    slow_sma = c.rolling(78).mean()  # ~1天趋势
    detrended = c - slow_sma
    x = detrended.dropna().values
    dx = np.diff(x); xl = x[:-1]
    m = ~np.isnan(xl) & ~np.isnan(dx)
    if m.sum() > 50:
        b = np.polyfit(xl[m], dx[m], 1)[0]
        theta = -b if b < 0 else 0.001
        hl_bars = np.log(2) / theta  # bars
        hl_min = hl_bars * 5  # → 分钟
    else:
        hl_min = 9999
    sh = _s(hl_min, [60, 180, 480], True)  # 1h好, 3h中, 8h差

    # A3: 超卖反弹概率
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

    score_mr = (sa + sh + sr) / 3

    # ── B. 可交易性 (0.25) ──
    avg_p = c.mean()
    avg_v = df["volume"].mean()
    dv = avg_p * avg_v
    sd = _s(dv, [5e8, 1e8, 3e7], False)

    # 完整度: 每天 78 根 5min K线
    trading_days = c.resample("D").last().dropna().count()
    expected_bars = trading_days * 78
    compl = min(len(df) / max(expected_bars, 1), 1.0)
    sc = _s(compl, [0.95, 0.85, 0.70], False)

    score_tr = (sd + sc) / 2

    # ── C. 风险 (0.15) ──
    peak = c.expanding().max()
    dd = abs((c - peak) / peak * 100).max()
    sdd = _s(dd, [15, 28, 45], True)

    d_open = df["open"].resample("D").first().dropna()
    d_close = c.resample("D").last().shift(1).dropna()
    ci = d_open.index.intersection(d_close.index)
    gr = (abs(d_open.loc[ci] - d_close.loc[ci]) / d_close.loc[ci] > 0.02).sum() / max(len(ci), 1)
    sg = _s(gr, [0.03, 0.08, 0.18], True)

    ur = (r > 0).sum() / len(r)
    ts = abs(ur - 0.5) * 2
    st = _s(ts, [0.12, 0.25, 0.45], True)

    score_risk = (sdd + sg + st) / 3

    # ── D. 交易机会 (0.20) ──
    sf = (z < W["z_thresh"]).sum() / max(len(z.dropna()), 1) * 100
    ss = _s(sf, [4, 1.5, 0.3], False)

    bo = []
    for i in range(len(z) - W["bounce"]):
        if z.iloc[i] < W["z_thresh"] and not pd.isna(z.iloc[i]):
            bo.append((c.iloc[i+W["bounce"]] - c.iloc[i]) / c.iloc[i] * 100)
    ab = np.mean(bo) if bo else 0
    sb = _s(ab, [1.0, 0.3, -0.1], False)

    ap = (_atr(df) / c * 100).mean()
    # 5min ATR: 0.15-0.8% 最优 (比15min的0.6-2.5%缩小)
    if ap < 0.15: sv_ap = 0.0
    elif ap < 0.8: sv_ap = 1.0 - abs(ap - 0.4) / 0.4
    else: sv_ap = max(0, 1.0 - (ap - 0.8) / 0.8)
    sv = sv_ap

    score_opp = (ss + sb + sv) / 3

    total = 0.40 * score_mr + 0.25 * score_tr + 0.15 * score_risk + 0.20 * score_opp
    if ap < 0.15: total -= 0.15

    return {
        "symbol": sym, "total": round(total, 3),
        "mr": round(score_mr, 3), "tr": round(score_tr, 3),
        "risk": round(score_risk, 3), "opp": round(score_opp, 3),
        "ac": round(ac_combo, 3), "hl_m": round(hl_min, 0),
        "rp": round(rp, 2), "dv_M": round(dv/1e6, 0),
        "ap": round(ap, 2), "dd": round(dd, 1), "gr": round(gr, 2),
        "sf": round(sf, 2), "ab": round(ab, 2), "n": len(df),
        "days": c.resample("D").last().dropna().count()
    }


def tier(t):
    if t >= 0.70: return "🟢A"
    if t >= 0.55: return "🟡B"
    if t >= 0.40: return "🟠C"
    return "🔴D"


if __name__ == "__main__":
    files = sorted(DATA_DIR.glob("*_1min.parquet"))
    print(f"📊 {len(files)} 只股票\n")

    results = [r for f in files if (r := score(f.stem.replace("_1min", ""), f))]

    results.sort(key=lambda x: -x["total"])

    hdr = f"{'股票':<6} {'总分':>5} {'档':>4} {'回归':>5} {'交易':>5} {'风险':>5} {'机会':>5} | {'自相关':>6} {'半衰m':>6} {'反弹':>5} {'成交M':>7} {'ATR%':>5} {'回撤%':>6} {'缺口':>4} {'信号%':>5} {'弹幅%':>5} {'天数':>5}"
    print(hdr)
    print("─" * 120)
    for r in results:
        print(f"{r['symbol']:<6} {r['total']:.3f} {tier(r['total']):>4} "
              f"{r['mr']:.3f} {r['tr']:.3f} {r['risk']:.3f} {r['opp']:.3f} | "
              f"{r['ac']:>6.3f} {r['hl_m']:>6.0f} {r['rp']:>5.2f} {r['dv_M']:>7.0f} "
              f"{r['ap']:>5.2f} {r['dd']:>6.1f} {r['gr']:>4.2f} "
              f"{r['sf']:>5.2f} {r['ab']:>5.2f} {r['days']:>5}")

    a = [r for r in results if r["total"] >= 0.70]
    b = [r for r in results if 0.55 <= r["total"] < 0.70]
    c = [r for r in results if 0.40 <= r["total"] < 0.55]
    print(f"\n🟢 A级({len(a)}): {[r['symbol'] for r in a]}")
    print(f"🟡 B级({len(b)}): {[r['symbol'] for r in b]}")
    print(f"🟠 C级({len(c)}): {[r['symbol'] for r in c]}")
