#!/usr/bin/env python3
"""
SOXL + KORU + MRVL  2026-06-16  理想邮件
表格化布局 + 买卖建议
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from debug.email_duplication_bug.analyze_warnings import load_day_data, zscore, calc_atr

SYMBOLS = ["SOXL", "KORU", "MRVL"]


# ═══════════════════════════════════════════════════
# 事件检测 (同之前，略精简)
# ═══════════════════════════════════════════════════

def find_flash_crashes(sym, df):
    close = df["close"].values.astype(float)
    dates = df["date"].values; n = len(df)
    ev = []; i = 20
    while i < n:
        z = zscore(close[:i+1], 20)
        if z < -3.0:
            bi, bz = i, z
            for j in range(i, min(n, i+10)):
                zj = zscore(close[:j+1], 20)
                if zj < bz: bz, bi = zj, j
            ws = max(0, bi-20)
            ev.append({"idx": bi, "sym": sym, "time": pd.Timestamp(dates[bi]).strftime("%H:%M"),
                       "type": "闪崩", "window": f"{pd.Timestamp(dates[ws]).strftime('%H:%M')}-{pd.Timestamp(dates[bi]).strftime('%H:%M')}",
                       "detail": f"${close[ws]:.2f}→${close[bi]:.2f} ({(close[bi]-close[ws])/close[ws]*100:+.1f}%) Z={bz:.1f}σ"})
            i = j + 15
        else: i += 1
    return ev

def find_price_surges(sym, df):
    close = df["close"].values.astype(float)
    dates = df["date"].values; n = len(df)
    ev = []
    for i in range(20, n):
        z = zscore(close[:i+1], 20)
        if z > 3.0:
            ws = max(0, i-20)
            ev.append({"idx": i, "sym": sym, "time": pd.Timestamp(dates[i]).strftime("%H:%M"),
                       "type": "暴涨", "window": f"{pd.Timestamp(dates[ws]).strftime('%H:%M')}-{pd.Timestamp(dates[i]).strftime('%H:%M')}",
                       "detail": f"${close[ws]:.2f}→${close[i]:.2f} (+{(close[i]-close[ws])/close[ws]*100:.1f}%) Z=+{z:.1f}σ"})
            i += 15
    return ev

def find_volume_spikes(sym, df):
    vol = df["volume"].values.astype(float)
    dates = df["date"].values; n = len(df)
    ev = []; merged = []
    for i in range(10, n):
        rv = float(np.mean(vol[i-4:i+1])); bv = float(np.mean(vol[i-9:i-4]))
        if bv > 0 and rv/bv > 5.0:
            merged.append({"idx": i, "sym": sym, "time": pd.Timestamp(dates[i]).strftime("%H:%M"),
                           "type": "放量", "window": f"{pd.Timestamp(dates[i-4]).strftime('%H:%M')}-{pd.Timestamp(dates[i]).strftime('%H:%M')}",
                           "detail": f"量比{rv/bv:.1f}x"})
    # 合并5分钟内
    result = []
    for e in merged:
        if not result or e["idx"] - result[-1]["idx"] > 5: result.append(e)
        elif float(e["detail"].split("x")[0][2:]) > float(result[-1]["detail"].split("x")[0][2:]): result[-1] = e
    return result

def find_bounces(sym, df):
    close = df["close"].values.astype(float); low = df["low"].values.astype(float)
    dates = df["date"].values; n = len(df)
    ev = []; i = 30
    while i < n:
        rl = float(np.min(low[:i+1])); price = close[i]
        if rl > 0 and price > rl*1.02 and (price-rl)/rl*100 > 3.0:
            li = np.argmin(low[:i+1]); ei = i
            for j in range(i+1, min(n, i+15)):
                if close[j] > close[ei]: ei = j
            bp = (close[ei]-rl)/rl*100
            if bp >= 3.0:
                ev.append({"idx": ei, "sym": sym, "time": pd.Timestamp(dates[ei]).strftime("%H:%M"),
                           "type": "反弹", "window": f"{pd.Timestamp(dates[li]).strftime('%H:%M')}-{pd.Timestamp(dates[ei]).strftime('%H:%M')}",
                           "detail": f"${rl:.2f}→${close[ei]:.2f} (+{bp:.1f}%)"})
            i = ei + 15; continue
        i += 1
    # 去重
    dedup = []
    for e in ev:
        st = e["window"].split("-")[0]
        if not dedup or st != dedup[-1]["window"].split("-")[0]: dedup.append(e)
        else:
            p = float(dedup[-1]["detail"].split("+")[1].split("%")[0])
            t = float(e["detail"].split("+")[1].split("%")[0])
            if t > p: dedup[-1] = e
    return dedup


def get_snapshot(sym, df, minute_idx):
    """获取某时刻的快照数据"""
    i = min(minute_idx, len(df)-1)
    c = df["close"].values.astype(float); h = df["high"].values.astype(float)
    l = df["low"].values.astype(float); o = df["open"].values.astype(float)
    v = df["volume"].values.astype(float)
    day_open = o[0]
    price = c[i]; pct = (price-day_open)/day_open*100
    dhigh = float(np.max(h[:i+1])); dlow = float(np.min(l[:i+1]))
    amp = (dhigh-dlow)/day_open*100

    # ATR & VWAP
    atr_v = calc_atr(h[:i+1], l[:i+1], c[:i+1], 14) if i >= 15 else price*0.03
    if i >= 5:
        wnd = min(i+1, 60)
        pw, vw = c[-wnd:], v[-wnd:]
        vwap = float(np.average(pw, weights=vw)) if vw.sum() > 0 else price
    else:
        vwap = price
    sma20 = float(np.mean(c[max(0,i-19):i+1]))

    dist_low = (price-dlow)/dlow*100 if dlow > 0 else 0

    # 状态判断
    states = []
    if pct < -3.0: states.append("下跌中")
    if dist_low < 1.0: states.append("近新低")
    if pct > 5.0: states.append("急涨")

    return {
        "price": price, "pct": pct, "day_high": dhigh, "day_low": dlow,
        "amp": amp, "atr": atr_v, "vwap": vwap, "sma20": sma20,
        "dist_low": dist_low, "states": states,
    }


def make_recommendation(sym, snap, events_nearby):
    """
    基于当前快照 + 附近事件, 生成买卖建议
    """
    price = snap["price"]
    atr = snap["atr"]
    vwap = snap["vwap"]
    sma20 = snap["sma20"]
    pct = snap["pct"]

    # 检查最近的事件
    recent_flash = [e for e in events_nearby if e["sym"] == sym and e["type"] == "闪崩"]
    recent_bounce = [e for e in events_nearby if e["sym"] == sym and e["type"] == "反弹"]
    recent_surge = [e for e in events_nearby if e["sym"] == sym and e["type"] == "暴涨"]

    recs = []

    # 买入建议
    if recent_flash:
        # 闪崩后抄底: 建议在 VWAP-1ATR 位置挂买单
        buy_price = round(vwap - 1.5 * atr, 2)
        if buy_price > snap["day_low"] * 1.01:
            buy_price = round(snap["day_low"] * 1.02, 2)
        stop_price = round(buy_price - 2.0 * atr, 2)
        target_price = round(sma20, 2)
        recs.append({
            "action": "🟢 买入", "sym": sym,
            "reason": f"闪崩后抄底",
            "entry": buy_price,
            "stop": stop_price,
            "target": target_price,
            "risk_reward": f"1:{(target_price-buy_price)/(buy_price-stop_price):.1f}" if buy_price > stop_price else "N/A",
        })
    elif pct < -5 and not recent_bounce:
        buy_price = round(snap["day_low"] * 1.01, 2)
        if buy_price <= price: buy_price = round(price * 1.005, 2)
        stop_price = round(buy_price - 2.0 * atr, 2)
        target_price = round(vwap, 2) if vwap > buy_price else round(sma20, 2)
        if target_price > buy_price:
            recs.append({
                "action": "🟢 买入", "sym": sym,
                "reason": f"急跌{pct:.0f}%, 均值回归",
                "entry": buy_price,
                "stop": stop_price,
                "target": target_price,
                "risk_reward": f"1:{(target_price-buy_price)/(buy_price-stop_price):.1f}" if buy_price > stop_price else "N/A",
            })

    # 反弹入场
    if recent_bounce:
        b = recent_bounce[-1]
        bp = float(b["detail"].split("+")[1].split("%")[0])
        if bp > 5:
            buy_price = round(price, 2)
            stop_price = round(snap["day_low"] * 0.98, 2)
            target_price = round(vwap, 2) if vwap > price else round(sma20, 2)
            if target_price > buy_price:
                recs.append({
                    "action": "🟢 买入", "sym": sym,
                    "reason": f"强反弹+{bp:.0f}%, 跟进",
                    "entry": buy_price,
                    "stop": stop_price,
                    "target": target_price,
                    "risk_reward": f"1:{(target_price-buy_price)/(buy_price-stop_price):.1f}" if buy_price > stop_price else "N/A",
                })

    # 卖出建议
    if recent_surge and pct > 3:
        sell_price = round(price, 2)
        recs.append({
            "action": "🔴 卖出", "sym": sym,
            "reason": f"急涨后建议减仓",
            "entry": sell_price,
            "stop": round(price + 1.0 * atr, 2),
            "target": round(vwap, 2),
            "risk_reward": "止盈",
        })
    elif pct > 5:
        recs.append({
            "action": "🔴 卖出", "sym": sym,
            "reason": f"日内涨幅过大, 追高风险",
            "entry": round(price, 2),
            "stop": "-",
            "target": "-",
            "risk_reward": "观望",
        })

    return recs


# ═══════════════════════════════════════════════════
# 邮件构建
# ═══════════════════════════════════════════════════

def build_email(all_data, all_events, trigger_minute, trigger_reason):
    lines = []
    ref_df = all_data[SYMBOLS[0]]
    i = min(trigger_minute, len(ref_df)-1)
    ts = pd.Timestamp(ref_df["date"].iloc[i]).strftime("%H:%M")

    # ── 头部 ──
    lines.append(f"⏰ {ts}  |  触发: {trigger_reason}")
    lines.append("")

    # ── 行情表格 ──
    lines.append("┌────────┬──────────┬────────┬────────┬────────┬────────┬──────────────┐")
    lines.append("│  股票   │   现价    │ 涨跌幅  │  最高   │  最低   │  振幅   │    状态       │")
    lines.append("├────────┼──────────┼────────┼────────┼────────┼────────┼──────────────┤")

    snapshots = {}
    for sym in SYMBOLS:
        snap = get_snapshot(sym, all_data[sym], trigger_minute)
        snapshots[sym] = snap
        state_str = ", ".join(snap["states"]) if snap["states"] else "正常"
        lines.append(
            f"│ {sym:6s} │ ${snap['price']:7.2f} │ "
            f"{snap['pct']:+6.1f}% │ ${snap['day_high']:6.2f} │ "
            f"${snap['day_low']:6.2f} │ {snap['amp']:5.1f}% │ "
            f"{state_str:12s} │"
        )
    lines.append("└────────┴──────────┴────────┴────────┴────────┴────────┴──────────────┘")
    lines.append("")

    # ── 近期事件 ──
    recent = [e for e in all_events if trigger_minute - 40 <= e["idx"] <= trigger_minute]
    if recent:
        lines.append("┌─ 近期事件 ─────────────────────────────────────────────────────┐")
        for e in sorted(recent, key=lambda x: x["idx"])[-8:]:
            emoji = {"闪崩": "⚡", "暴涨": "⚡", "反弹": "🟢", "放量": "📊"}.get(e["type"], "•")
            lines.append(f"│ {emoji} {e['sym']:5s} {e['type']}  [{e['window']}]  {e['detail']:40s} │")
        lines.append("└──────────────────────────────────────────────────────────────┘")
        lines.append("")

    # ── 买卖建议表格 ──
    all_recs = []
    for sym in SYMBOLS:
        nearby = [e for e in all_events if e["sym"] == sym and trigger_minute - 60 <= e["idx"] <= trigger_minute]
        recs = make_recommendation(sym, snapshots[sym], nearby)
        all_recs.extend(recs)

    if all_recs:
        lines.append("┌─ 操作建议 ──────────────────────────────────────────────────────────────────────┐")
        lines.append("│ 股票   │ 方向   │ 入场价     │ 止损价     │ 止盈价     │ 盈亏比  │ 理由              │")
        lines.append("├────────┼────────┼───────────┼───────────┼───────────┼─────────┼──────────────────┤")
        for r in all_recs:
            lines.append(
                f"│ {r['sym']:6s} │ {r['action']:6s} │ "
                f"${r['entry']} ".ljust(10) + "│ "
                f"${r['stop']} ".ljust(10) + "│ "
                f"${r['target']} ".ljust(10) + "│ "
                f"{r['risk_reward']:7s} │ "
                f"{r['reason']:16s} │"
            )
        lines.append("└────────┴────────┴───────────┴───────────┴───────────┴─────────┴──────────────────┘")
    else:
        lines.append("  💡 当前无明确操作建议，观望为主")
        lines.append("")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════
# 触发决策
# ═══════════════════════════════════════════════════

def decide_triggers(all_events):
    """只保留真正重要的触发点"""
    triggers = []

    # 闪崩/暴涨 → 即时
    for e in all_events:
        if e["type"] in ("闪崩", "暴涨"):
            triggers.append((e["idx"], f"{e['sym']} {e['type']}", "instant"))

    # 强反弹 (>5%) → 即时
    for e in all_events:
        if e["type"] == "反弹":
            pct = float(e["detail"].split("+")[1].split("%")[0])
            if pct > 4.5:
                triggers.append((e["idx"], f"{e['sym']} 强反弹+{pct:.0f}%", "instant"))

    # 放量(>10x) → 即时
    for e in all_events:
        if e["type"] == "放量":
            ratio = float(e["detail"].split("x")[0][2:])
            if ratio > 10:
                triggers.append((e["idx"], f"{e['sym']} 巨量{ratio:.0f}x", "instant"))

    # 每30分钟例行更新
    for mi in range(30, 390, 30):
        triggers.append((mi, f"例行更新", "routine"))

    triggers.sort()

    # 合并5分钟内
    merged = []
    for t in triggers:
        if not merged or t[0] - merged[-1][0] > 8:
            merged.append(t)
        else:
            old = merged[-1]
            new_p = "instant" if old[2] == "instant" or t[2] == "instant" else "routine"
            new_r = old[1] if old[2] == "instant" else (t[1] if t[2] == "instant" else f"{old[1]}, {t[1]}")
            merged[-1] = (old[0], new_r, new_p)

    # 去重: 例行邮件间隔<25分钟跳过
    filtered = []
    for t in merged:
        if t[2] == "routine" and filtered and t[0] - filtered[-1][0] < 30:
            continue
        filtered.append(t)

    return filtered


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════

all_data = {}
all_events = []

for sym in SYMBOLS:
    df = load_day_data(sym)
    all_data[sym] = df
    for e in find_flash_crashes(sym, df): all_events.append(e)
    for e in find_price_surges(sym, df): all_events.append(e)
    for e in find_volume_spikes(sym, df): all_events.append(e)
    for e in find_bounces(sym, df): all_events.append(e)

all_events.sort(key=lambda x: x["idx"])

triggers = decide_triggers(all_events)

out = Path("debug/email_duplication_bug/output/SOXL_KORU_MRVL_2026-06-16_emails.txt")
with open(out, "w") as f:
    f.write("╔══════════════════════════════════════════════════════════════╗\n")
    f.write("║  SOXL + KORU + MRVL  2026-06-16  盘中监测                    ║\n")
    f.write(f"║  全天 {len(triggers)} 封邮件                                               ║\n")
    f.write("╚══════════════════════════════════════════════════════════════╝\n")
    f.write("\n图例: ⚡闪崩/暴涨  🟢低位反弹  📊放量异常\n")
    f.write("建议: 🟢买入(抄底/反弹跟进)  🔴卖出(急涨减仓)\n\n")

    for i, (mi, reason, priority) in enumerate(triggers, 1):
        content = build_email(all_data, all_events, mi, reason)
        f.write(content + "\n\n")

    # 事件汇总表
    f.write("╔══════════════════════════════════════════════════════════════════════╗\n")
    f.write("║  全天事件汇总                                                        ║\n")
    f.write("╠═══════╤═══════╤══════════════════╤══════════════════════════════════╣\n")
    f.write("║ 时间   │ 股票   │ 类型              │ 详情                             ║\n")
    f.write("╟───────┼───────┼──────────────────┼──────────────────────────────────╢\n")
    for e in all_events:
        f.write(f"║ {e['time']:5s} │ {e['sym']:5s} │ {e['type']:16s} │ {e['detail']:32s} ║\n")
    f.write("╚═══════╤═══════╧══════════════════╧══════════════════════════════════╝\n")

print(f"已写入: {out}")
print(f"全天 {len(triggers)} 封邮件, {len(all_events)} 个事件")

for i, (mi, reason, p) in enumerate(triggers, 1):
    icon = "🔴" if p == "instant" else "🔵"
    ts = pd.Timestamp(all_data[SYMBOLS[0]]["date"].iloc[min(mi, len(all_data[SYMBOLS[0]])-1)]).strftime("%H:%M")
    print(f"  {icon} #{i:2d}  {ts}  {reason}")
