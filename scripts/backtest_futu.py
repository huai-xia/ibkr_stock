#!/usr/bin/env python3
"""
短线均值回归策略回测 — 基于富途 13 天 1min K 线数据
重点测试: ARM 不同 ATR 止损乘数
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.indicators import add_all
from src.analysis.anomaly import AnomalyDetector

# ── 配置 ──
STOCKS = ["MRVL", "SOXL", "ARM", "KORU"]
DATA_DIR = Path("data/minutes")
CAPITAL_PER_TRADE = 1000  # 每笔交易资金
ATR_MULTIPLIERS = [2.0, 2.5, 3.0, 3.5]  # 测试多种止损乘数

# ── 加载数据 ──
def load_all_days(symbol: str) -> pd.DataFrame:
    """加载某股票所有日期的 1min 数据"""
    files = sorted(DATA_DIR.glob(f"{symbol}_2026-06-*_1min.parquet"))
    frames = []
    for f in files:
        df = pd.read_parquet(f)
        # 处理两种列名: date (新) 或 timestamp (旧)
        time_col = None
        for c in ["date", "timestamp"]:
            if c in df.columns:
                time_col = c
                break
        if time_col:
            df = df.copy()
            df[time_col] = pd.to_datetime(df[time_col])
            df = df.set_index(time_col)
        elif isinstance(df.index, pd.DatetimeIndex):
            pass  # 已经是日期索引
        frames.append(df)
    if frames:
        result = pd.concat(frames)
        result = result.sort_index()
        result = result[~result.index.duplicated(keep='first')]
        return result
    return pd.DataFrame()


# ── 均值回归策略（逐分钟模拟） ──
def simulate_mean_reversion(
    df: pd.DataFrame, symbol: str,
    atr_stop_mult: float = 2.0,
    cool_minutes: int = 10,
    trend_filter: str = "sma_slope",  # None / "sma_slope" / "price_above_sma" / "both"
    open_filter_min: int = 0,  # 开盘后跳过分钟数 (0=不过滤)
) -> dict:
    """
    均值回归策略模拟

    入场条件:
        Z-Score < -2.5  AND  RSI < 35  AND  close <= BB_lower × 1.01
        + 趋势过滤 (可选): SMA斜率向上 / 价格>SMA
        + 开盘过滤 (可选): 跳过开盘 N 分钟

    出场条件:
        止损: entry - ATR × multiplier
        止盈: EMA×60% + VWAP×25% + SMA×15%
        收盘平仓: 15:50

    风控:
        冷却: 同一股票触发后冷却 N 分钟
    """
    df = df.copy()
    df = add_all(df)  # 添加所有指标

    # 补充: Z-Score, EMA_20, VWAP_20 (add_all 里没有)
    df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
    rolling_std = df["close"].rolling(20).std()
    df["zscore_20"] = (df["close"] - df["sma_20"]) / rolling_std.replace(0, np.nan)
    # 滚动 VWAP
    typ_price = (df["high"] + df["low"] + df["close"]) / 3
    vol_x_price = (typ_price * df["volume"]).rolling(20).sum()
    vol_sum = df["volume"].rolling(20).sum()
    df["vwap_20"] = (vol_x_price / vol_sum.replace(0, np.nan))

    detector = AnomalyDetector()

    trades = []
    position = None  # {'entry_price', 'entry_time', 'stop', 'target'}
    cooldown_until = None

    for i in range(20, len(df)):  # 跳过前 20 分钟让指标热身
        row = df.iloc[i]
        now = df.index[i]

        # 冷却检查
        if cooldown_until and now < cooldown_until:
            continue

        # 已有持仓 — 检查退出
        if position:
            exit_price = None
            exit_reason = ""

            # 止损
            if row["low"] <= position["stop"]:
                exit_price = position["stop"]
                exit_reason = "止损"
            # 止盈
            elif row["high"] >= position["target"]:
                exit_price = position["target"]
                exit_reason = "止盈"
            # 收盘强制平仓（15:50 后）
            elif now.hour >= 15 and now.minute >= 50:
                exit_price = row["close"]
                exit_reason = "收盘平仓"

            if exit_price:
                pnl = (exit_price - position["entry_price"]) / position["entry_price"] * 100
                trades.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "exit_time": now,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "pnl_pct": round(pnl, 2),
                    "pnl_dollar": round(CAPITAL_PER_TRADE * pnl / 100, 2),
                    "reason": exit_reason,
                    "atr_mult": atr_stop_mult,
                })
                position = None
                cooldown_until = now + pd.Timedelta(minutes=cool_minutes)
            continue

        # 无持仓 — 检查买入信号
        zscore = row.get("zscore_20", 0)
        rsi14 = row.get("rsi_14", 50)
        bb_lower = row.get("bb_lower", 0)
        close = row["close"]
        atr14 = row.get("atr_14", 0)
        sma20 = row.get("sma_20", 0)

        # 基础条件
        if not (zscore < -2.5 and rsi14 < 35 and close <= bb_lower * 1.01):
            continue
        if atr14 <= 0:
            continue

        # 趋势过滤: 只过滤强下跌趋势，允许正常回调
        # 均值回归买的是回调，所以 price < SMA 是正常的
        # 需要过滤的是 SMA 持续大幅下行（强趋势下跌）
        if trend_filter:
            trend_ok = True  # 默认放行
            if i >= 10:
                sma_now = row.get("sma_20", 0)
                sma_10ago = df.iloc[i - 10].get("sma_20", 0)
                if sma_now > 0 and sma_10ago > 0:
                    sma_slope_pct = (sma_now - sma_10ago) / sma_10ago * 100  # 10分钟变化率

                    if trend_filter == "sma_slope":
                        # 只过滤 SMA20 10分钟内下降 > 0.3% 的强下跌趋势
                        trend_ok = sma_slope_pct > -0.3
                    elif trend_filter == "sma_slope_strict":
                        trend_ok = sma_slope_pct > -0.15
            # 数据不足时放行

            if not trend_ok:
                continue

        # 开盘过滤: 跳过盘初高波动期
        if open_filter_min > 0:
            minutes_since_open = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
            if 0 < minutes_since_open < open_filter_min:
                continue

        entry_price = close
        stop_loss = close - atr14 * atr_stop_mult

        # 综合目标: EMA60% + 滚动VWAP20(25%) + SMA20(15%)
        ema20 = row.get("ema_20", close)
        vwap20 = row.get("vwap_20", close)
        sma20_for_target = row.get("sma_20", close)
        target = ema20 * 0.60 + vwap20 * 0.25 + sma20_for_target * 0.15

        if target <= entry_price * 1.002:
            continue  # 止盈空间不足 0.2%

        position = {
            "entry_price": entry_price,
            "entry_time": now,
            "stop": round(stop_loss, 2),
            "target": round(target, 2),
        }

    # 如果最后还有持仓，按最后价平仓
    if position:
        last_price = df.iloc[-1]["close"]
        pnl = (last_price - position["entry_price"]) / position["entry_price"] * 100
        trades.append({
            "symbol": symbol,
            "entry_time": position["entry_time"],
            "exit_time": df.index[-1],
            "entry_price": position["entry_price"],
            "exit_price": last_price,
            "pnl_pct": round(pnl, 2),
            "pnl_dollar": round(CAPITAL_PER_TRADE * pnl / 100, 2),
            "reason": "收盘平仓(未触发)",
            "atr_mult": atr_stop_mult,
        })

    return _summarize(trades, symbol, atr_stop_mult)


def _summarize(trades: list, symbol: str, atr_mult: float) -> dict:
    """汇总结果"""
    if not trades:
        return {
            "symbol": symbol, "atr_mult": atr_mult,
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "total_pnl": 0.0,
            "avg_pnl_pct": 0.0, "max_win": 0.0, "max_loss": 0.0,
            "trades": [],
        }
    wins = [t for t in trades if t["pnl_dollar"] > 0]
    losses = [t for t in trades if t["pnl_dollar"] <= 0]
    total_pnl = sum(t["pnl_dollar"] for t in trades)
    return {
        "symbol": symbol,
        "atr_mult": atr_mult,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_pct": round(np.mean([t["pnl_pct"] for t in trades]), 2),
        "max_win": round(max(t["pnl_dollar"] for t in trades), 2),
        "max_loss": round(min(t["pnl_dollar"] for t in trades), 2),
        "trades": trades,
    }


# ── 主流程 ──
if __name__ == "__main__":
    print("=" * 72)
    print("  短线均值回归策略回测 — 富途 13 天 1min K 线")
    print("=" * 72)

    # ── Part 1: 趋势过滤对比 ──
    print("\n🔬 趋势过滤效果对比 (默认 ATR=2.0×, 无开盘过滤)\n")
    print(f"  {'过滤方式':<20} {'MRVL':>10} {'SOXL':>10} {'ARM':>10} {'KORU':>10}")
    print(f"  {'─'*20} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")

    trend_modes = [
        (None, "无过滤"),
        ("sma_slope", "SMA斜率>-0.3%"),
        ("sma_slope_strict", "SMA斜率>-0.15%"),
    ]
    trend_results = {}
    for mode, label in trend_modes:
        row = [f"  {label:<20}"]
        for sym in STOCKS:
            df = load_all_days(sym)
            if df.empty:
                row.append(f"{'—':>10}")
                continue
            r = simulate_mean_reversion(df, sym, atr_stop_mult=2.0, trend_filter=mode)
            key = f"{sym}_{mode}"
            trend_results[key] = r
            row.append(f"{r['total_pnl']:>+8.1f} ({r['win_rate']:.0f}%)")
        print("".join(row))

    # ── Part 2: ARM 专项 ──
    print("\n" + "=" * 72)
    print("  🔬 ARM 组合优化: 趋势过滤 + ATR乘数")
    print("=" * 72)
    df_arm = load_all_days("ARM")
    print(f"\n  {'ATR':<6} {'过滤':<18} {'笔数':>4} {'胜率':>6} {'盈亏':>8} {'最大亏损':>8}")
    print(f"  {'─'*6} {'─'*18} {'─'*4} {'─'*6} {'─'*8} {'─'*8}")
    for mult in [2.0, 2.5, 3.0, 3.5]:
        for mode, label in trend_modes:
            r = simulate_mean_reversion(df_arm, "ARM", atr_stop_mult=mult, trend_filter=mode)
            emoji = "🟢" if r["total_pnl"] > 0 else "🔴"
            print(f"  {emoji} {mult}×  {label:<18} {r['total_trades']:>4} {r['win_rate']:>5.1f}% ${r['total_pnl']:>+7.1f} ${r['max_loss']:>+7.1f}")

    # ── Part 3: 所有股票最佳组合 ──
    print("\n" + "=" * 72)
    print("  📈 各股票最佳参数组合 (趋势过滤 + ATR乘数)")
    print("=" * 72)
    print(f"  {'股票':<6} {'趋势过滤':<18} {'ATR':<6} {'笔数':>4} {'胜率':>6} {'盈亏':>8}")
    print(f"  {'─'*6} {'─'*18} {'─'*6} {'─'*4} {'─'*6} {'─'*8}")
    for sym in STOCKS:
        df = load_all_days(sym)
        if df.empty:
            continue
        best = None
        best_label = ""
        for mult in ATR_MULTIPLIERS:
            for mode, label in trend_modes:
                r = simulate_mean_reversion(df, sym, atr_stop_mult=mult, trend_filter=mode)
                if best is None or r["total_pnl"] > best["total_pnl"]:
                    best = r
                    best_label = f"{label}, {mult}×"
        print(f"  {sym:<6} {best_label:<18} {best['atr_mult']:<6.1f} {best['total_trades']:>4} {best['win_rate']:>5.1f}% ${best['total_pnl']:>+7.1f}")

    print("\n✅ 回测完成")
