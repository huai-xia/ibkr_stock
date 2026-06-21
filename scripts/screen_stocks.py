#!/usr/bin/env python3
"""
股票筛选器：基于 1h K 线计算 4 维度评分，选出适合均值回归的标的
方法: docs/04-analysis/stock-screening.md
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

DATA_DIR = Path("data/hourly")

# ── 指标计算 ──

def compute_scores(df: pd.DataFrame, symbol: str) -> dict:
    """计算单只股票的所有筛选指标和综合评分"""
    if df.empty or len(df) < 50:
        return None

    df = df.copy()
    df = df.set_index("date").sort_index()
    close = df["close"].astype(float)
    returns = np.log(close / close.shift(1)).dropna()
    if len(returns) < 30:
        return None

    # ── A. 均值回归性 (35%) ──
    # A1: 收益率自相关
    autocorr = returns.autocorr(lag=1)
    s_autocorr = _score_range(autocorr, [-0.05, -0.02, 0.0], lower_is_better=True)

    # A2: OU 回归半衰期
    x = close.values
    dx = np.diff(x)
    x_lag = x[:-1]
    mask = ~np.isnan(x_lag) & ~np.isnan(dx)
    if np.sum(mask) > 10:
        b = np.polyfit(x_lag[mask], dx[mask], 1)[0]
        theta = -b if b < 0 else 0.01
        halflife = np.log(2) / theta / 6.5  # 转换为交易日
    else:
        halflife = 999
    s_halflife = _score_range(halflife, [0.5, 2, 5], lower_is_better=True)

    # A3: 超卖后反弹概率
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    z = (close - sma20) / std20.replace(0, np.nan)
    # 找 Z < -2 的事件，统计 5 小时内回到均值的概率
    recovery_count = 0
    event_count = 0
    for i in range(len(z) - 5):
        if z.iloc[i] < -2 and not pd.isna(z.iloc[i]):
            event_count += 1
            # 未来 5 小时是否回到 SMA
            if (close.iloc[i+1:i+6] > sma20.iloc[i]).any():
                recovery_count += 1
    recovery_prob = recovery_count / event_count if event_count > 0 else 0
    s_recovery = _score_range(recovery_prob, [0.5, 0.35, 0.2], lower_is_better=False)

    score_mean_rev = (s_autocorr + s_halflife + s_recovery) / 3

    # ── B. 可交易性 (25%) ──
    # B1: 日均成交额
    avg_price = close.mean()
    avg_vol = df["volume"].mean()
    dollar_vol = avg_price * avg_vol
    s_dollar_vol = _score_range(dollar_vol, [5e8, 1e8, 5e7], lower_is_better=False)

    # B2: 波动率适中度
    atr = _calc_atr(df, 14)
    atr_pct = (atr / close * 100).mean()
    vol_deviation = abs(atr_pct - 0.5)  # 距离最优值 0.5% 的偏差
    s_vol_fit = _score_range(vol_deviation, [0.2, 0.5, 1.0], lower_is_better=True)

    # B3: 数据完整度
    expected = len(pd.date_range(df.index.min(), df.index.max(), freq="1h"))
    actual = len(df)
    completeness = min(actual / expected, 1.0) if expected > 0 else 0
    s_complete = _score_range(completeness, [0.95, 0.85, 0.7], lower_is_better=False)

    score_tradable = (s_dollar_vol + s_vol_fit + s_complete) / 3

    # ── C. 风险 (25%) ──
    # C1: 趋势强度 (ADX 替代: 用趋势一致性)
    up_ratio = (returns > 0).sum() / len(returns)
    trend_strength = abs(up_ratio - 0.5) * 2
    s_adx = _score_range(trend_strength, [0.15, 0.3, 0.5], lower_is_better=True)

    # C2: 最大回撤
    peak = close.expanding().max()
    drawdown = (close - peak) / peak * 100
    max_dd = abs(drawdown.min())
    s_drawdown = _score_range(max_dd, [10, 20, 35], lower_is_better=True)

    # C3: 缺口风险
    daily_open = df["open"].resample("D").first().dropna()
    daily_prev_close = close.resample("D").last().shift(1).dropna()
    common_idx = daily_open.index.intersection(daily_prev_close.index)
    gaps = abs(daily_open.loc[common_idx] - daily_prev_close.loc[common_idx]) / daily_prev_close.loc[common_idx]
    gap_risk = (gaps > 0.02).sum() / len(gaps) if len(gaps) > 0 else 0
    s_gap = _score_range(gap_risk, [0.03, 0.08, 0.15], lower_is_better=True)

    score_risk = (s_adx + s_drawdown + s_gap) / 3

    # ── D. 交易机会 (15%) ──
    # D1: 超卖频率
    signal_freq = (z < -2.5).sum() / len(z.dropna()) * 100
    s_signal = _score_range(signal_freq, [4, 2, 0.5], lower_is_better=False)

    # D2: 平均反弹幅度
    bounce_returns = []
    for i in range(len(z) - 5):
        if z.iloc[i] < -2.5 and not pd.isna(z.iloc[i]):
            ret_5h = (close.iloc[i+5] - close.iloc[i]) / close.iloc[i] * 100
            bounce_returns.append(ret_5h)
    avg_bounce = np.mean(bounce_returns) if bounce_returns else 0
    s_bounce = _score_range(avg_bounce, [0.8, 0.3, 0.0], lower_is_better=False)

    score_oppty = (s_signal + s_bounce) / 2

    # ── 综合评分 ──
    total = 0.35 * score_mean_rev + 0.25 * score_tradable + 0.25 * score_risk + 0.15 * score_oppty

    return {
        "symbol": symbol,
        "total": round(total, 3),
        "mean_rev": round(score_mean_rev, 3),
        "tradable": round(score_tradable, 3),
        "risk": round(score_risk, 3),
        "oppty": round(score_oppty, 3),
        "autocorr": round(autocorr, 3),
        "halflife_days": round(halflife, 1),
        "recovery_prob": round(recovery_prob, 2),
        "dollar_vol_M": round(dollar_vol / 1e6, 0),
        "atr_pct": round(atr_pct, 2),
        "trend_strength": round(trend_strength, 2),
        "max_dd_pct": round(max_dd, 1),
        "gap_risk": round(gap_risk, 2),
        "signal_freq_pct": round(signal_freq, 2),
        "avg_bounce_pct": round(avg_bounce, 2),
        "bars": len(df),
    }


def _calc_atr(df, period=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


def _score_range(value, thresholds, lower_is_better=False):
    """将指标映射到 [0, 1] 分数
    thresholds: [满分阈值, 半分阈值, 0分阈值]
    """
    t_full, t_half, t_zero = thresholds
    if lower_is_better:
        if value <= t_full: return 1.0
        if value <= t_half: return 0.5 + 0.5 * (t_half - value) / (t_half - t_full)
        if value <= t_zero: return 0.5 * (t_zero - value) / (t_zero - t_half)
        return 0.0
    else:
        if value >= t_full: return 1.0
        if value >= t_half: return 0.5 + 0.5 * (value - t_half) / (t_full - t_half)
        if value >= t_zero: return 0.5 * (value - t_zero) / (t_half - t_zero)
        return 0.0


def tier(total: float) -> str:
    if total >= 0.70: return "🟢 A"
    if total >= 0.55: return "🟡 B"
    if total >= 0.40: return "🟠 C"
    return "🔴 D"


# ── 主流程 ──

def main():
    files = sorted(DATA_DIR.glob("*_1h.parquet"))
    print(f"📊 加载 {len(files)} 只股票数据...\n")

    results = []
    for f in files:
        sym = f.stem.replace("_1h", "")
        df = pd.read_parquet(f)
        r = compute_scores(df, sym)
        if r:
            results.append(r)

    results.sort(key=lambda x: -x["total"])

    # 打印
    print(f"{'股票':<6} {'总分':>5} {'档':>4} {'均值回归':>6} {'可交易':>6} {'风险':>6} {'机会':>6} | {'自相关':>6} {'半衰期':>6} {'反弹率':>6} {'成交额M':>8} {'ATR%':>5} {'趋势':>5} {'回撤%':>6} {'缺口':>4}")
    print("─" * 130)
    for r in results:
        print(f"{r['symbol']:<6} {r['total']:.3f} {tier(r['total']):>4} "
              f"{r['mean_rev']:.3f} {r['tradable']:.3f} {r['risk']:.3f} {r['oppty']:.3f} | "
              f"{r['autocorr']:>6.3f} {r['halflife_days']:>6.1f} {r['recovery_prob']:>6.2f} "
              f"{r['dollar_vol_M']:>8.0f} {r['atr_pct']:>5.2f} "
              f"{r['trend_strength']:>5.2f} {r['max_dd_pct']:>6.1f} {r['gap_risk']:>4.2f}")

    # 统计
    a_count = sum(1 for r in results if r["total"] >= 0.70)
    b_count = sum(1 for r in results if 0.55 <= r["total"] < 0.70)
    print(f"\n📊 A级:{a_count} B级:{b_count} C级:{len(results)-a_count-b_count-len([r for r in results if r['total']<0.40])} D级:{len([r for r in results if r['total']<0.40])}")

    print("\n🟢 A级 (优先):", [r["symbol"] for r in results if r["total"] >= 0.70])
    print("🟡 B级 (备用):", [r["symbol"] for r in results if 0.55 <= r["total"] < 0.70])


if __name__ == "__main__":
    main()
