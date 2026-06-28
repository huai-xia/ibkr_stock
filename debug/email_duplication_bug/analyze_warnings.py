#!/usr/bin/env python3
"""
交易日警告检测 + 抄底交易模拟
使用 2026-06-16 的 1分钟 K 线数据，模拟全天盘中实时检测 + 交易
输出: 走势图(标注告警+买卖点) + 交易统计

策略:
  买入: 盘中急跌 >5% OR 闪电崩盘 → 抄底
  卖出: 动态止盈 (SMA20+0.5%) OR 盘中急涨 >5%
  规则: 同一时间只持有一笔，买入后等待卖出信号
"""

import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

# ── 中文字体 ──
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "STFangsong", "LiHei Pro"]
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

DATA_DIR = Path("data/minutes")
OUTPUT_DIR = Path("debug/email_duplication_bug/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SYMBOLS = ["AAOX", "AAPL", "AEIS", "ARM", "KORU", "MRVL", "SOXL"]

SYM_COLORS = {
    "AAOX": "#e74c3c", "AAPL": "#3498db", "AEIS": "#2ecc71",
    "ARM": "#9b59b6", "KORU": "#f39c12", "MRVL": "#1abc9c",
    "SOXL": "#e67e22",
}

THRESHOLDS = {
    "zscore_crash": -3.0, "zscore_surge": 3.0,
    "vwap_deviation": 3.0, "vol_spike": 5.0,
    "intraday_drop": -3.0, "intraday_rise": 5.0,
    "bounce_from_low": 3.0, "near_low_pct": 1.0,
}
COOLDOWN_MINUTES = 10


# ═══════════════════════════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════════════════════════

def load_day_data(symbol: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{symbol}_2026-06-16_1min.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ═══════════════════════════════════════════════════════════════
# 指标计算
# ═══════════════════════════════════════════════════════════════

def zscore(series: np.ndarray, window: int) -> float:
    if len(series) < window: return 0.0
    w = series[-window:]
    mu, std = float(np.mean(w)), float(np.std(w))
    return float((series[-1] - mu) / std) if std > 0 else 0.0

def calc_atr(high, low, close, period=14):
    if len(close) < period + 1:
        return float(np.mean(high - low))
    tr = [max(float(high[j])-float(low[j]),
              abs(float(high[j])-float(close[j-1])),
              abs(float(low[j])-float(close[j-1])))
          for j in range(-period, 0)]
    return float(np.mean(tr))


# ═══════════════════════════════════════════════════════════════
# 检测 + 交易模拟 (合并，避免重复计算)
# ═══════════════════════════════════════════════════════════════

def detect_and_trade(symbol: str, df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """
    逐分钟运行: 告警检测 + 交易信号。
    Returns: (alerts, trades)
      trades: [{idx, time, type:"buy"/"sell", price, reason, profit, pnl_pct}]
    """
    alerts = []
    trades = []
    n = len(df)
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    volume = df["volume"].values.astype(float)
    timestamps = df["date"].values
    day_open = float(df["open"].iloc[0])

    cooldown = {}         # 告警冷却
    has_position = False  # 是否持仓
    entry_price = 0.0

    for i in range(20, n):
        ts = pd.Timestamp(timestamps[i])
        ts_str = ts.strftime("%H:%M")
        price = float(close[i])

        c_vis = close[:i+1]
        h_vis = high[:i+1]
        l_vis = low[:i+1]
        v_vis = volume[:i+1]

        running_low = float(np.min(l_vis))
        intraday_pct = (price - day_open) / day_open * 100 if day_open > 0 else 0

        def can_trigger(atype): return (i - cooldown.get(atype, -999)) >= COOLDOWN_MINUTES

        def trigger(atype, level, reason, metric, strength=""):
            if not can_trigger(atype): return None
            cooldown[atype] = i
            a = {"time": ts_str, "idx": i, "price": round(price, 2),
                 "alert_type": atype, "level": level,
                 "reason": reason, "metric": round(metric, 2)}
            if strength: a["strength"] = strength
            alerts.append(a)
            return a

        # ── 告警检测 (同之前) ──
        z20 = zscore(c_vis, 20)
        if z20 < THRESHOLDS["zscore_crash"]:
            trigger("flash_crash", "critical" if z20 < -4 else "warning",
                    f"闪电崩盘 Z={z20:.1f}σ", z20)
        elif z20 > THRESHOLDS["zscore_surge"]:
            trigger("price_surge", "critical" if z20 > 4 else "warning",
                    f"瞬间暴涨 Z=+{z20:.1f}σ", z20)

        if i >= 5:
            wnd = min(i+1, 30)
            pw, vw = c_vis[-wnd:], v_vis[-wnd:]
            if vw.sum() > 0:
                vwap = float(np.average(pw, weights=vw))
                vwap_dev = (price - vwap) / vwap * 100
                if abs(vwap_dev) > THRESHOLDS["vwap_deviation"]:
                    direction = "高于" if vwap_dev > 0 else "低于"
                    trigger("vwap_deviation", "warning",
                            f"VWAP偏离 {vwap_dev:+.1f}% ({direction}均价)", vwap_dev)

        if i >= 10:
            rv = float(np.mean(v_vis[-5:])); bv = float(np.mean(v_vis[-10:-5]))
            vr = rv / bv if bv > 0 else 1.0
            if vr > THRESHOLDS["vol_spike"]:
                trigger("volume_spike", "warning", f"成交量异常 (量比 {vr:.1f}x)", vr)

        if i >= 16:
            roc15 = (c_vis[-1] - c_vis[-16]) / c_vis[-16] * 100
            if roc15 < -5 and close[i] > close[i-5]:
                trigger("reversal", "info",
                        f"跌后反弹 (15分钟跌{roc15:.1f}%, 5分钟回升)", roc15)

        if i >= 30:
            h30 = float(np.max(c_vis[-30:-1]))
            if price > h30 * 1.01:
                rv5 = float(np.mean(v_vis[-5:]))
                bv30 = float(np.mean(v_vis[-30:-5]))
                if rv5 > bv30 * 2:
                    trigger("breakout", "info",
                            f"放量突破30分钟高点 ${h30:.2f}", price)

        if intraday_pct < THRESHOLDS["intraday_drop"]:
            trigger("intraday_sharp_drop",
                    "critical" if intraday_pct < -5 else "warning",
                    f"盘中急跌 {intraday_pct:+.1f}%", intraday_pct,
                    "strong" if intraday_pct < -5 else "medium")

        if intraday_pct > THRESHOLDS["intraday_rise"]:
            trigger("intraday_sharp_rise",
                    "critical" if intraday_pct > 8 else "warning",
                    f"盘中急涨 {intraday_pct:+.1f}%", intraday_pct,
                    "strong" if intraday_pct > 8 else "medium")

        if running_low > 0 and price > running_low * 1.02:
            bp = (price - running_low) / running_low * 100
            if bp > THRESHOLDS["bounce_from_low"]:
                trigger("bounce_from_low", "info",
                        f"低位反弹 {bp:.1f}% (从${running_low:.2f})", bp)

        if running_low > 0 and price < running_low * 1.01:
            trigger("near_day_low", "warning",
                    f"接近日内新低 ${running_low:.2f}", round((price-running_low)/running_low*100, 2))

        if i >= 30:
            atr_v = calc_atr(high[:i+1], low[:i+1], close[:i+1], 14)
            if atr_v > 0:
                sma20 = float(np.mean(c_vis[-20:]))
                target = sma20 * 1.005
                if price >= target:
                    trigger("dynamic_take_profit", "info",
                            f"达到动态止盈 ${target:.2f}", target)
                # 止损线 (仅用于告警, 不做交易卖出)
                if price <= float(np.average(c_vis, weights=v_vis)) - 2.0*atr_v:
                    trigger("dynamic_stop_loss", "critical",
                            f"触发动态止损", 0.0)

        # ═══════════════════════════════════════
        # 交易信号
        # ═══════════════════════════════════════

        # BUY: 盘中急跌 >5% OR 闪电崩盘 (且无持仓)
        is_crash = intraday_pct < -5
        is_flash = z20 < THRESHOLDS["zscore_crash"]
        is_vwap_dip = (i >= 5 and vw.sum() > 0 and
                       (price - float(np.average(pw, weights=vw))) / float(np.average(pw, weights=vw)) * 100 < -5)

        buy_signal = (is_crash or is_flash or is_vwap_dip)
        if buy_signal and not has_position and can_trigger("trade_entry"):
            cooldown["trade_entry"] = i
            has_position = True
            entry_price = price
            reason_parts = []
            if is_crash: reason_parts.append(f"急跌{intraday_pct:+.1f}%")
            if is_flash: reason_parts.append(f"闪崩Z={z20:.1f}")
            if is_vwap_dip: reason_parts.append(f"VWAP超跌")
            trades.append({
                "idx": i, "time": ts_str, "type": "buy",
                "price": round(price, 2), "reason": " + ".join(reason_parts),
                "profit": 0, "pnl_pct": 0,
            })

        # SELL: 动态止盈 OR 急涨 >5% (有持仓时)
        is_tp = (i >= 30 and price >= float(np.mean(c_vis[-20:])) * 1.005)
        is_surge = intraday_pct > 5
        sell_signal = is_tp or is_surge
        if sell_signal and has_position and can_trigger("trade_exit"):
            cooldown["trade_exit"] = i
            has_position = False
            profit = price - entry_price
            pnl_pct = profit / entry_price * 100
            reason_parts = []
            if is_tp: reason_parts.append(f"止盈")
            if is_surge: reason_parts.append(f"急涨{intraday_pct:+.1f}%")
            trades.append({
                "idx": i, "time": ts_str, "type": "sell",
                "price": round(price, 2),
                "reason": " + ".join(reason_parts),
                "profit": round(profit, 2), "pnl_pct": round(pnl_pct, 2),
            })

    # 收盘强制平仓
    if has_position:
        i = n - 1
        ts_str = pd.Timestamp(timestamps[i]).strftime("%H:%M")
        price = float(close[i])
        profit = price - entry_price
        pnl_pct = profit / entry_price * 100
        trades.append({
            "idx": i, "time": ts_str, "type": "sell",
            "price": round(price, 2), "reason": "收盘平仓",
            "profit": round(profit, 2), "pnl_pct": round(pnl_pct, 2),
        })

    return alerts, trades


# ═══════════════════════════════════════════════════════════════
# 单股票走势图 + 买卖点
# ═══════════════════════════════════════════════════════════════

ALERT_STYLES = {
    "flash_crash": ("v", "#8B0000", "闪电崩盘"),
    "price_surge": ("^", "#FF4500", "瞬间暴涨"),
    "vwap_deviation": ("s", "#FF8C00", "VWAP偏离"),
    "volume_spike": ("D", "#9370DB", "量异常"),
    "reversal": ("o", "#228B22", "跌后反弹"),
    "breakout": ("P", "#006400", "放量突破"),
    "intraday_sharp_drop": ("v", "#FF0000", "盘中急跌"),
    "intraday_sharp_rise": ("^", "#FF6347", "盘中急涨"),
    "bounce_from_low": ("o", "#32CD32", "低位反弹"),
    "near_day_low": ("s", "#FFD700", "接近新低"),
    "dynamic_stop_loss": ("X", "#000000", "动态止损"),
    "dynamic_take_profit": ("P", "#00CED1", "动态止盈"),
}


def plot_single_stock(symbol: str, df: pd.DataFrame,
                      alerts: list[dict], trades: list[dict]):
    fig, axes = plt.subplots(3, 1, figsize=(18, 13),
                              gridspec_kw={'height_ratios': [3, 1, 1]}, sharex=True)
    color = SYM_COLORS.get(symbol, "#333")

    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    open_ = df["open"].values.astype(float)
    volume = df["volume"].values.astype(float)
    dates = df["date"].values
    day_open = open_[0]

    # ── 子图1: 价格 + 告警 + 买卖点 ──
    ax1 = axes[0]
    ax1.fill_between(range(len(df)), low, high, alpha=0.10, color=color)
    ax1.plot(range(len(df)), close, color=color, linewidth=1.2, label="收盘价", zorder=3)
    sma20 = pd.Series(close).rolling(20).mean()
    ax1.plot(range(len(df)), sma20, color="orange", linewidth=1.0, alpha=0.7,
             linestyle="--", label="SMA20")

    # 告警标注 (小标记)
    by_type = defaultdict(list)
    for a in alerts:
        by_type[a["alert_type"]].append(a)
    for atype, aa in by_type.items():
        if atype not in ALERT_STYLES: continue
        mk, clr, label_cn = ALERT_STYLES[atype]
        xs = [a["idx"] for a in aa]; ys = [a["price"] for a in aa]
        ax1.scatter(xs, ys, c=clr, marker=mk, s=45, zorder=8,
                    edgecolors="white", linewidth=0.3, label=label_cn, alpha=0.75)

    # ── 买卖点 (大标记 + 文字) ──
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]

    if buys:
        bx = [t["idx"] for t in buys]; by = [t["price"] for t in buys]
        ax1.scatter(bx, by, c="#00C853", marker="^", s=200, zorder=20,
                    edgecolors="#004D1A", linewidth=2.0, label=f"买入 ({len(buys)}次)")
        # 标注买入价
        for t in buys:
            ax1.annotate(f"买${t['price']:.2f}",
                         (t["idx"], t["price"]),
                         xytext=(0, 18), textcoords="offset points",
                         fontsize=7, color="#006400", fontweight="bold",
                         ha="center",
                         bbox=dict(boxstyle="round,pad=0.2", fc="#a5d6a7", ec="none", alpha=0.85))

    if sells:
        sx = [t["idx"] for t in sells]; sy = [t["price"] for t in sells]
        ax1.scatter(sx, sy, c="#FF1744", marker="v", s=200, zorder=20,
                    edgecolors="#7F0000", linewidth=2.0, label=f"卖出 ({len(sells)}次)")
        for t in sells:
            pnl_str = f"+${t['profit']:.2f}" if t['profit'] > 0 else f"-${abs(t['profit']):.2f}"
            ax1.annotate(f"卖${t['price']:.2f}\n{pnl_str}",
                         (t["idx"], t["price"]),
                         xytext=(0, -22), textcoords="offset points",
                         fontsize=7, color="#7F0000" if t['profit'] <= 0 else "#006400",
                         fontweight="bold", ha="center",
                         bbox=dict(boxstyle="round,pad=0.2",
                                   fc="#ffcdd2" if t['profit'] <= 0 else "#c8e6c9",
                                   ec="none", alpha=0.85))

    ax1.set_ylabel("价格 ($)", fontsize=11)
    ax1.grid(True, alpha=0.25)

    # 图例
    handles, labels = ax1.get_legend_handles_labels()
    unique = dict(zip(labels, handles))
    ax1.legend(unique.values(), unique.keys(), loc="upper right", fontsize=7, ncol=2)

    # ── 子图2: 成交量 ──
    ax2 = axes[1]
    bar_c = [color if close[i] >= open_[i] else "#e74c3c" for i in range(len(df))]
    ax2.bar(range(len(df)), volume, color=bar_c, alpha=0.5, width=1.0)
    ax2.set_ylabel("成交量", fontsize=11)
    ax2.grid(True, alpha=0.25)

    # ── 子图3: 日内涨跌幅 ──
    ax3 = axes[2]
    pct = (close - day_open) / day_open * 100
    ax3.fill_between(range(len(df)), 0, pct, where=(pct >= 0),
                     color="#2ecc71", alpha=0.2)
    ax3.fill_between(range(len(df)), 0, pct, where=(pct < 0),
                     color="#e74c3c", alpha=0.2)
    ax3.plot(range(len(df)), pct, color=color, linewidth=1.0)
    ax3.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
    ax3.axhline(y=-5, color="red", linewidth=0.8, linestyle=":", alpha=0.6, label="买入线 -5%")
    ax3.axhline(y=3, color="green", linewidth=0.8, linestyle=":", alpha=0.6, label="急涨线 +5%")
    ax3.set_ylabel("涨跌幅 %", fontsize=11)
    ax3.set_xlabel("时间", fontsize=11)
    ax3.grid(True, alpha=0.25)
    ax3.legend(fontsize=8)

    # 买卖点在涨跌幅图上也标
    if buys:
        ax3.scatter(bx, [pct[i] for i in bx], c="#00C853", marker="^",
                    s=80, zorder=15, edgecolors="#004D1A", linewidth=1.5)
    if sells:
        ax3.scatter(sx, [pct[i] for i in sx], c="#FF1744", marker="v",
                    s=80, zorder=15, edgecolors="#7F0000", linewidth=1.5)

    # X轴时间
    n_ticks = min(8, len(df))
    tick_idx = np.linspace(0, len(df)-1, n_ticks, dtype=int)
    tick_labels = [pd.Timestamp(dates[i]).strftime("%H:%M") for i in tick_idx]
    for ax in axes:
        ax.set_xticks(tick_idx)
    ax3.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=9)

    # ── 交易统计 ──
    buy_count = len(buys)
    completed = buy_count  # 每次买入都有对应卖出
    total_pnl = sum(t.get("profit", 0) for t in sells)
    win_count = sum(1 for t in sells if t.get("profit", 0) > 0)

    change_pct = (close[-1] - day_open) / day_open * 100
    fig.suptitle(
        f"{symbol}  —  2026-06-16  |  "
        f"开${day_open:.2f} → 收${close[-1]:.2f} ({change_pct:+.2f}%)  |  "
        f"交易{completed}笔  "
        f"总盈亏 ${total_pnl:+.2f}  "
        f"胜率 {win_count}/{completed}",
        fontsize=14, fontweight="bold", color=SYM_COLORS.get(symbol, "#333"))

    plt.tight_layout()
    sp = OUTPUT_DIR / f"{symbol}_2026-06-16_warnings.png"
    fig.savefig(sp, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return sp


# ═══════════════════════════════════════════════════════════════
# 汇总图
# ═══════════════════════════════════════════════════════════════

def plot_summary(all_data, all_alerts, all_trades):
    fig, axes = plt.subplots(2, 1, figsize=(20, 12))

    # 子图1: 所有股票日内涨跌幅
    ax1 = axes[0]
    for sym in SYMBOLS:
        if sym not in all_data: continue
        df = all_data[sym]
        c = df["close"].values.astype(float)
        o0 = float(df["open"].iloc[0])
        pct = (c - o0) / o0 * 100
        ax1.plot(range(len(pct)), pct, color=SYM_COLORS.get(sym, "#333"),
                 linewidth=1.5, label=sym, alpha=0.85)
    ax1.axhline(y=0, color="gray", linewidth=0.8, linestyle="--")
    ax1.axhline(y=-5, color="red", linewidth=0.8, linestyle=":", alpha=0.5, label="买入线")
    ax1.set_ylabel("日内涨跌幅 %", fontsize=13)
    ax1.set_title("全部股票日内走势 + 交易统计 (2026-06-16)", fontsize=14, fontweight="bold")
    ax1.legend(fontsize=10, ncol=4)
    ax1.grid(True, alpha=0.3)

    # 子图2: 交易统计柱状图
    ax2 = axes[1]
    syms_data = [s for s in SYMBOLS if s in all_trades]
    x_pos = np.arange(len(syms_data))
    widths = 0.35

    profits = []
    trade_counts = []
    win_rates = []
    for sym in syms_data:
        trs = all_trades.get(sym, [])
        sells = [t for t in trs if t["type"] == "sell"]
        pnl = sum(t.get("profit", 0) for t in sells)
        n_trades = len([t for t in trs if t["type"] == "buy"])
        wins = sum(1 for t in sells if t.get("profit", 0) > 0)
        profits.append(pnl)
        trade_counts.append(n_trades)
        win_rates.append(wins / n_trades * 100 if n_trades > 0 else 0)

    bars1 = ax2.bar(x_pos - widths/2, profits, widths, color=["#2ecc71" if p > 0 else "#e74c3c" for p in profits],
                    edgecolor="white", linewidth=0.5, label="总盈亏 ($)")
    ax2.set_ylabel("总盈亏 ($)", fontsize=12)
    ax2.axhline(y=0, color="gray", linewidth=0.5)

    # 标注数值
    for i, (p, n, w) in enumerate(zip(profits, trade_counts, win_rates)):
        ax2.text(i - widths/2, p + (3 if p >= 0 else -8),
                 f"${p:+.2f}\n{n}笔\n胜率{w:.0f}%",
                 ha="center", fontsize=8, fontweight="bold",
                 color="#006400" if p > 0 else "#7F0000")

    ax2_twin = ax2.twinx()
    ax2_twin.bar(x_pos + widths/2, trade_counts, widths, color="#3498db", alpha=0.5,
                 edgecolor="white", linewidth=0.5, label="交易次数")
    ax2_twin.set_ylabel("交易次数", fontsize=12)

    ax2.set_xticks(x_pos)
    ax2.set_xticklabels(syms_data, fontsize=11, fontweight="bold")
    ax2.set_title("交易盈亏 & 次数 汇总", fontsize=14, fontweight="bold")

    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2_twin.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)

    plt.tight_layout()
    sp = OUTPUT_DIR / "summary_all_stocks.png"
    fig.savefig(sp, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return sp


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("IBKR 交易日警告 + 抄底交易模拟")
    print("日期: 2026-06-16 | 股票: " + ", ".join(SYMBOLS))
    print("策略: 急跌>5%/闪崩→买入 | 止盈/急涨>5%→卖出")
    print("=" * 70)

    all_data = {}
    all_alerts = {}
    all_trades = {}

    for sym in SYMBOLS:
        print(f"\n{'─'*55}")
        print(f"  {sym}")
        print(f"{'─'*55}")
        df = load_day_data(sym)
        if df is None: continue
        all_data[sym] = df

        op, cp = float(df["open"].iloc[0]), float(df["close"].iloc[-1])
        hp, lp = float(df["high"].max()), float(df["low"].min())
        chg = (cp - op) / op * 100; amp = (hp - lp) / op * 100
        print(f"  开盘 ${op:.2f} → 收盘 ${cp:.2f}  ({chg:+.2f}%)  |  "
              f"振幅 {amp:.1f}%  |  高 ${hp:.2f}  低 ${lp:.2f}")

        alerts, trades = detect_and_trade(sym, df)
        all_alerts[sym] = alerts
        all_trades[sym] = trades

        # 告警统计
        by_type = defaultdict(list)
        for a in alerts: by_type[a["alert_type"]].append(a)
        print(f"  告警: {len(alerts)} 条")
        for atype, aa in sorted(by_type.items(), key=lambda x: -len(x[1]))[:4]:
            cn = ALERT_STYLES.get(atype, ("?", "#999", atype))[2]
            print(f"    {cn}: {len(aa)}条  {aa[0]['time']}→{aa[-1]['time']}")

        # 交易统计
        buys = [t for t in trades if t["type"] == "buy"]
        sells = [t for t in trades if t["type"] == "sell"]
        total_pnl = sum(t.get("profit", 0) for t in sells)
        wins = sum(1 for t in sells if t.get("profit", 0) > 0)

        print(f"\n  💰 交易: {len(buys)}笔买入, {len(sells)}笔卖出")
        for i, (b, s) in enumerate(zip(buys, sells)):
            pnl = s.get("profit", 0)
            emoji = "✅" if pnl > 0 else "❌"
            print(f"    {emoji} #{i+1}: {b['time']} 买 ${b['price']:.2f} ({b['reason']})"
                  f" → {s['time']} 卖 ${s['price']:.2f} ({s['reason']})"
                  f"  |  盈亏 ${pnl:+.2f} ({s.get('pnl_pct', 0):+.2f}%)")
        print(f"    总盈亏: ${total_pnl:+.2f}  |  胜率: {wins}/{len(buys)}"
              f" ({wins/len(buys)*100:.0f}%)" if buys else "")

        sp = plot_single_stock(sym, df, alerts, trades)
        print(f"  📈 {sp}")

    # 汇总
    print(f"\n{'='*70}")
    print(f"全市场汇总")
    print(f"{'='*70}")

    total_trades = 0
    total_pnl_all = 0
    for sym in SYMBOLS:
        trs = all_trades.get(sym, [])
        buys = [t for t in trs if t["type"] == "buy"]
        sells = [t for t in trs if t["type"] == "sell"]
        pnl = sum(t.get("profit", 0) for t in sells)
        n = len(buys)
        w = sum(1 for t in sells if t.get("profit", 0) > 0)
        alerts_n = len(all_alerts.get(sym, []))
        bar = "█" * min(alerts_n//4, 25) if alerts_n > 0 else ""
        print(f"  {sym:6s}: 告警{alerts_n:3d}条 | 交易{n}笔 | "
              f"盈亏${pnl:+.2f} | 胜率{w}/{n} | {bar}")
        total_trades += n
        total_pnl_all += pnl

    print(f"  {'─'*55}")
    print(f"  合计: {total_trades}笔交易 | 总盈亏 ${total_pnl_all:+.2f}")

    sp = plot_summary(all_data, all_alerts, all_trades)
    print(f"\n📊 汇总图: {sp}")

    print(f"\n所有文件: {OUTPUT_DIR.resolve()}/")
    for f in sorted(OUTPUT_DIR.iterdir()):
        print(f"  {f.name}")


if __name__ == "__main__":
    main()
