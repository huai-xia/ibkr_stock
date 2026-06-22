#!/usr/bin/env python3
"""60天均值回归回测 — IBKR 1min数据"""

import sys; sys.path.insert(0, '.')
import pandas as pd; import numpy as np
from pathlib import Path
from src.strategy.indicators import add_all

DATA_DIR = Path("data/ibkr_1min")
CAPITAL = 1000  # 每笔$1000
COOL = 10       # 冷却10分钟
TREND_FILTER = True  # SMA斜率>-0.3%

# 每只股票的最优ATR乘数 (来自之前13天回测)
ATR_MAP = {
    'MRVL': 2.5, 'SOXL': 3.0, 'ARM': 3.5, 'KORU': 3.5,
    # 其他用默认2.5
}

def backtest(sym, atr_mult=2.5, recent_days=60):
    df = pd.read_parquet(DATA_DIR / f"{sym}_1min.parquet")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # 取最近60个交易日
    all_dates = sorted(set(df.index.date))
    if len(all_dates) > recent_days:
        df = df[df.index.date >= all_dates[-recent_days]]

    if len(df) < 500:
        return None

    # 计算指标
    df = add_all(df)
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    std = df["close"].rolling(20).std()
    df["zscore_20"] = (df["close"] - df["sma_20"]) / std.replace(0, np.nan)
    typ = (df["high"] + df["low"] + df["close"]) / 3
    vp = (typ * df["volume"]).rolling(20).sum()
    vs = df["volume"].rolling(20).sum()
    df["vwap_20"] = vp / vs.replace(0, np.nan)

    trades = []
    pos = None; cool_until = None
    skipped_trend = 0

    for i in range(20, len(df)):
        row = df.iloc[i]; now = df.index[i]

        if cool_until and now < cool_until:
            continue

        if pos:
            exit_p = None; reason = ""
            if row["low"] <= pos["stop"]: exit_p = pos["stop"]; reason = "止损"
            elif row["high"] >= pos["target"]: exit_p = pos["target"]; reason = "止盈"
            elif now.hour >= 15 and now.minute >= 50: exit_p = row["close"]; reason = "收盘"

            if exit_p:
                pnl = (exit_p - pos["entry"]) / pos["entry"] * 100
                trades.append({
                    "entry_time": pos["entry_time"], "exit_time": now,
                    "entry": pos["entry"], "exit": exit_p,
                    "pnl_pct": round(pnl, 2),
                    "pnl": round(CAPITAL * pnl / 100, 2),
                    "reason": reason
                })
                pos = None
                cool_until = now + pd.Timedelta(minutes=COOL)
            continue

        # 入场信号
        zs = row.get("zscore_20", 0); rsi = row.get("rsi_14", 50)
        bb = row.get("bb_lower", 0); atr14 = row.get("atr_14", 0); close = row["close"]

        if not (zs < -2.5 and rsi < 35 and close <= bb * 1.01):
            continue
        if atr14 <= 0:
            continue

        # 趋势过滤
        if TREND_FILTER and i >= 10:
            s10 = df.iloc[i-10].get("sma_20", 0)
            s0 = row.get("sma_20", 0)
            if s0 > 0 and s10 > 0 and (s0 - s10) / s10 <= -0.003:
                skipped_trend += 1
                continue

        entry = close
        stop = close - atr14 * atr_mult
        target = row["ema_20"] * 0.6 + row.get("vwap_20", close) * 0.25 + row["sma_20"] * 0.15

        if target <= entry * 1.002:
            continue

        pos = {"entry": entry, "entry_time": now, "stop": round(stop, 2), "target": round(target, 2)}

    # 未平仓按最后价
    if pos:
        last = df.iloc[-1]["close"]
        pnl = (last - pos["entry"]) / pos["entry"] * 100
        trades.append({
            "entry_time": pos["entry_time"], "exit_time": df.index[-1],
            "entry": pos["entry"], "exit": last,
            "pnl_pct": round(pnl, 2),
            "pnl": round(CAPITAL * pnl / 100, 2),
            "reason": "持仓"
        })

    if not trades:
        return None

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)

    return {
        "symbol": sym, "n": len(trades),
        "wr": round(len(wins) / len(trades) * 100, 1),
        "pnl": round(total_pnl, 1),
        "avg_win": round(np.mean([t["pnl"] for t in wins]), 1) if wins else 0,
        "avg_loss": round(np.mean([t["pnl"] for t in losses]), 1) if losses else 0,
        "max_w": round(max(t["pnl"] for t in trades), 1),
        "max_l": round(min(t["pnl"] for t in trades), 1),
        "skipped": skipped_trend,
        "atr": atr_mult,
    }


if __name__ == "__main__":
    files = sorted(DATA_DIR.glob("*_1min.parquet"))
    results = []

    for f in files:
        sym = f.stem.replace("_1min", "")
        atr = ATR_MAP.get(sym, 2.5)
        r = backtest(sym, atr_mult=atr, recent_days=60)
        if r:
            results.append(r)

    results.sort(key=lambda x: -x["pnl"])

    print(f"\n{'='*80}")
    print(f"  均值回归策略 60天回测 (最近60交易日, ATR乘数个性化, 趋势过滤)")
    print(f"{'='*80}\n")
    print(f"{'股票':<6} {'交易':>4} {'胜率':>6} {'总盈亏':>8} {'均盈':>7} {'均亏':>7} {'最佳':>7} {'最差':>7} {'过滤':>4} {'ATR':>4}")
    print(f"{'─'*75}")

    total_pnl = 0
    total_trades = 0
    for r in results:
        emoji = "🟢" if r["pnl"] > 50 else ("🟡" if r["pnl"] > 0 else ("🟠" if r["pnl"] > -30 else "🔴"))
        print(f"{emoji} {r['symbol']:<4} {r['n']:>4} {r['wr']:>5.1f}% ${r['pnl']:>+7.1f} ${r['avg_win']:>+6.1f} ${r['avg_loss']:>+6.1f} ${r['max_w']:>+6.1f} ${r['max_l']:>+6.1f} {r['skipped']:>4} {r['atr']:>4.1f}")
        total_pnl += r["pnl"]
        total_trades += r["n"]

    print(f"{'─'*75}")
    print(f"  合计: {total_trades}笔交易, 总盈亏 ${total_pnl:+.1f}, 平均 ${total_pnl/total_trades:+.1f}/笔")

    # 月度估算
    monthly = total_pnl / 3  # 60天≈3个月
    print(f"  月均: ${monthly:+.1f} (假设每笔$1000)")

    print(f"\n🟢 推荐交易:")
    for r in results:
        if r["pnl"] > 0 and r["wr"] >= 45:
            print(f"  {r['symbol']}: {r['n']}笔, 胜率{r['wr']}%, 盈亏${r['pnl']:+.0f}")
