#!/usr/bin/env python3
"""
MRVL 6月16日 理想邮件输出模拟
三层告警框架: 日内趋势 / 持续状态 / 离散事件
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from debug.email_duplication_bug.analyze_warnings import load_day_data, zscore

SYM = "MRVL"
df = load_day_data(SYM)
close = df["close"].values.astype(float)
high = df["high"].values.astype(float)
low = df["low"].values.astype(float)
open_ = df["open"].values.astype(float)
volume = df["volume"].values.astype(float)
dates = df["date"].values
day_open = open_[0]
n = len(df)


# ═══════════════════════════════════════════════════════
# 第3层: 离散事件检测
# ═══════════════════════════════════════════════════════

def find_flash_crashes():
    """闪电崩盘: 20分钟 Z < -3, 合并相邻"""
    events = []
    i = 20
    while i < n:
        z = zscore(close[:i+1], 20)
        if z < -3.0:
            # 找到Z最低点作为事件中心
            best_i = i
            best_z = z
            j = i
            while j < n and j < i + 10:  # 向后看10分钟
                zj = zscore(close[:j+1], 20)
                if zj < best_z:
                    best_z = zj
                    best_i = j
                j += 1
            # 确定窗口: 往前20分钟
            w_start = max(0, best_i - 20)
            events.append({
                "time": pd.Timestamp(dates[best_i]).strftime("%H:%M"),
                "type": "⚡ 闪电崩盘",
                "window": f"{pd.Timestamp(dates[w_start]).strftime('%H:%M')} → {pd.Timestamp(dates[best_i]).strftime('%H:%M')}",
                "detail": f"${close[w_start]:.2f} → ${close[best_i]:.2f} ({(close[best_i]-close[w_start])/close[w_start]*100:+.1f}%), Z={best_z:.1f}σ",
                "idx": best_i,
            })
            i = j + 15  # 跳过已处理区间
        else:
            i += 1
    return events


def find_volume_spikes():
    """成交量异常: 5分钟量比 > 5x"""
    events = []
    for i in range(10, n):
        rv = float(np.mean(volume[i-4:i+1]))
        bv = float(np.mean(volume[i-9:i-4]))
        if bv > 0:
            ratio = rv / bv
            if ratio > 5.0:
                events.append({
                    "time": pd.Timestamp(dates[i]).strftime("%H:%M"),
                    "type": "📊 放量异常",
                    "window": f"{pd.Timestamp(dates[i-4]).strftime('%H:%M')} → {pd.Timestamp(dates[i]).strftime('%H:%M')}",
                    "detail": f"量比 {ratio:.1f}x (近5分钟 {volume[i-4:i+1].sum():.0f} vs 基准 {volume[i-9:i-4].sum():.0f})",
                    "idx": i,
                })
    # 合并5分钟内相邻的
    merged = []
    for e in events:
        if not merged or e["idx"] - merged[-1]["idx"] > 5:
            merged.append(e)
        else:
            # 保留量比更大的
            if float(e["detail"].split("x")[0].split()[-1]) > float(merged[-1]["detail"].split("x")[0].split()[-1]):
                merged[-1] = e
    return merged


def find_bounce_segments():
    """
    低位反弹段: 从日内低点起, 价格连续回升 >3%
    找到反弹的起点和终点, 以及涨幅
    """
    events = []
    i = 30
    while i < n:
        running_low = float(np.min(low[:i+1]))
        price = close[i]
        if running_low > 0 and price > running_low * 1.02:
            bp = (price - running_low) / running_low * 100
            if bp > 3.0:
                # 找到反弹起点 (日内最低点的时间)
                low_idx = np.argmin(low[:i+1])
                # 反弹终点 (当前或继续往后找更高点)
                end_idx = i
                j = i + 1
                while j < min(n, i + 15):
                    if close[j] > close[end_idx]:
                        end_idx = j
                    j += 1

                end_price = close[end_idx]
                total_bounce = (end_price - running_low) / running_low * 100
                if total_bounce >= 3.0:
                    events.append({
                        "time": pd.Timestamp(dates[end_idx]).strftime("%H:%M"),
                        "type": "🟢 低位反弹",
                        "window": f"{pd.Timestamp(dates[low_idx]).strftime('%H:%M')} → {pd.Timestamp(dates[end_idx]).strftime('%H:%M')}",
                        "detail": f"${running_low:.2f} → ${end_price:.2f} (+{total_bounce:.1f}%), 日内低点反弹",
                        "idx": end_idx,
                    })
                i = end_idx + 15
                continue
        i += 1

    # 去重: 同一起点只保留最大反弹
    deduped = []
    for e in events:
        start_t = e["window"].split("→")[0].strip()
        if not deduped or start_t != deduped[-1]["window"].split("→")[0].strip():
            deduped.append(e)
        else:
            # 保留涨幅更大的
            prev_pct = float(deduped[-1]["detail"].split("+")[1].split("%")[0])
            this_pct = float(e["detail"].split("+")[1].split("%")[0])
            if this_pct > prev_pct:
                deduped[-1] = e
    return deduped


# ═══════════════════════════════════════════════════════
# 第2层: 持续状态追踪
# ═══════════════════════════════════════════════════════

def track_states():
    """
    追踪整天的状态变化:
    - 持续下跌: 距开盘跌幅 >3% 开始, <1% 结束
    - 新低附近: 现价距日内最低 <1%
    """
    states = []  # [{start_time, end_time, state, detail}]
    in_downtrend = False
    downtrend_start = 0
    downtrend_worst = 0.0
    downtrend_worst_idx = 0
    in_near_low = False
    near_low_start = 0

    for i in range(30, n):
        price = close[i]
        intraday_pct = (price - day_open) / day_open * 100
        running_low = float(np.min(low[:i+1]))
        dist_to_low = (price - running_low) / running_low * 100

        # 持续下跌
        if intraday_pct < -3.0:
            if not in_downtrend:
                in_downtrend = True
                downtrend_start = i
                downtrend_worst = intraday_pct
                downtrend_worst_idx = i
            if intraday_pct < downtrend_worst:
                downtrend_worst = intraday_pct
                downtrend_worst_idx = i
        else:
            if in_downtrend and intraday_pct > -1.0:
                # 下跌结束
                states.append({
                    "state": "持续下跌",
                    "start": pd.Timestamp(dates[downtrend_start]).strftime("%H:%M"),
                    "end": pd.Timestamp(dates[i]).strftime("%H:%M"),
                    "detail": (f"${day_open:.2f} → ${close[downtrend_worst_idx]:.2f} "
                              f"(最大跌幅 {downtrend_worst:.1f}%), "
                              f"反弹至 ${price:.2f}"),
                    "start_idx": downtrend_start,
                    "end_idx": i,
                })
                in_downtrend = False

        # 新低附近
        if dist_to_low < 1.0:
            if not in_near_low:
                in_near_low = True
                near_low_start = i
        else:
            if in_near_low and dist_to_low > 2.0:
                states.append({
                    "state": "新低附近",
                    "start": pd.Timestamp(dates[near_low_start]).strftime("%H:%M"),
                    "end": pd.Timestamp(dates[i]).strftime("%H:%M"),
                    "detail": f"最低 ${running_low:.2f}, 现价 ${price:.2f} (已脱离 +{dist_to_low:.1f}%)",
                    "start_idx": near_low_start,
                    "end_idx": i,
                })
                in_near_low = False

    # 收盘时仍在持续的状态
    if in_downtrend:
        states.append({
            "state": "持续下跌",
            "start": pd.Timestamp(dates[downtrend_start]).strftime("%H:%M"),
            "end": pd.Timestamp(dates[n-1]).strftime("%H:%M"),
            "detail": (f"${day_open:.2f} → ${close[-1]:.2f} "
                      f"(最大跌幅 {downtrend_worst:.1f}%), 仍在下跌中"),
            "start_idx": downtrend_start,
            "end_idx": n-1,
        })
    if in_near_low:
        states.append({
            "state": "新低附近",
            "start": pd.Timestamp(dates[near_low_start]).strftime("%H:%M"),
            "end": pd.Timestamp(dates[n-1]).strftime("%H:%M"),
            "detail": f"最低 ${np.min(low):.2f}, 收盘 ${close[-1]:.2f}, 仍在低点附近",
            "start_idx": near_low_start,
            "end_idx": n-1,
        })

    return states


# ═══════════════════════════════════════════════════════
# 邮件发送决策
# ═══════════════════════════════════════════════════════

def decide_emails(flash_crashes, vol_spikes, bounces, states):
    """
    决策何时发邮件:
    - 离散事件触发时 → 即时发送
    - 状态变化时 → 即时发送
    - 每30分钟例行更新 (如果没有其他触发)
    """
    triggers = []  # [(minute_idx, trigger_reason, priority)]

    # 离散事件
    for e in flash_crashes:
        triggers.append((e["idx"], f"闪电崩盘 {e['time']}", "instant"))
    for e in vol_spikes:
        triggers.append((e["idx"], f"放量异常 {e['time']}", "instant"))
    # 反弹事件 → 合并到最近的例行邮件, 除非反弹 >5% (强信号)
    for e in bounces:
        pct = float(e["detail"].split("+")[1].split("%")[0])
        if pct > 5:
            triggers.append((e["idx"], f"强反弹 {e['time']}", "instant"))
        else:
            triggers.append((e["idx"], f"低位反弹 {e['time']}", "routine"))

    # 状态开始/结束
    for s in states:
        triggers.append((s["start_idx"], f"状态开始: {s['state']} {s['start']}", "instant"))
        triggers.append((s["end_idx"], f"状态结束: {s['state']} {s['end']}", "routine"))

    # 例行检查: 每30分钟
    for mi in range(30, 390, 30):
        triggers.append((mi, f"例行更新 {mi//60+9:02d}:{mi%60:02d}", "routine"))

    # 按时间排序, 合并5分钟内多个触发
    triggers.sort()
    merged = []
    for t in triggers:
        if not merged or t[0] - merged[-1][0] > 5:
            merged.append(t)
        else:
            # 保留更高优先级的触发原因, 合并原因
            old = merged[-1]
            new_priority = "instant" if (old[2] == "instant" or t[2] == "instant") else "routine"
            new_reason = old[1]
            if t[2] == "instant" and old[2] != "instant":
                new_reason = t[1] + " | " + old[1]
            merged[-1] = (old[0], new_reason, new_priority)

    # 去重: 如果例行更新距离上次发送不到20分钟且无新事件, 跳过
    filtered = []
    for i, t in enumerate(merged):
        if t[2] == "routine" and filtered and t[0] - filtered[-1][0] < 20:
            continue
        filtered.append(t)

    return filtered


# ═══════════════════════════════════════════════════════
# 构建每封邮件的内容
# ═══════════════════════════════════════════════════════

def build_email_content(trigger_minute):
    """根据触发时刻, 构建邮件内容 (只能看到该时刻之前的数据)"""
    i = min(trigger_minute, n-1)
    price = close[i]
    ts_str = pd.Timestamp(dates[i]).strftime("%H:%M")
    intraday_pct = (price - day_open) / day_open * 100
    day_high = float(np.max(high[:i+1]))
    day_low = float(np.min(low[:i+1]))

    lines = []
    lines.append(f"⏰ {ts_str}")
    lines.append("")

    # ── 第1层: 日内趋势 ──
    amp = (day_high - day_low) / day_open * 100 if day_open > 0 else 0
    lines.append(f"┌─ MRVL 日内趋势 ───────────────────────────────")
    lines.append(f"│ 开盘 ${day_open:.2f} → 现价 ${price:.2f}  ({intraday_pct:+.1f}%)")
    lines.append(f"│ 最高 ${day_high:.2f}  最低 ${day_low:.2f}  振幅 {amp:.1f}%")
    lines.append(f"└──────────────────────────────────────────────")
    lines.append("")

    # ── 第2层: 活跃状态 ──
    active_states = []
    if intraday_pct < -3.0:
        # 找到下跌开始时间
        for j in range(i, 20, -1):
            if (close[j] - day_open) / day_open * 100 > -1.0:
                start_t = pd.Timestamp(dates[min(j+1, i)]).strftime("%H:%M")
                break
        else:
            start_t = pd.Timestamp(dates[20]).strftime("%H:%M")
        active_states.append(
            f"⚠️ 持续下跌 [{start_t} → 现在]\n"
            f"   ${day_open:.2f} → ${price:.2f}, 累计 {intraday_pct:.1f}%"
        )

    dist_low = (price - day_low) / day_low * 100 if day_low > 0 else 100
    if dist_low < 1.0:
        active_states.append(
            f"⚠️ 运行在新低附近\n"
            f"   最低 ${day_low:.2f}, 现价距新低仅 {dist_low:.2f}%"
        )

    if active_states:
        lines.append("┌─ 进行中的状态 ───────────────────────────────")
        for s in active_states:
            for l in s.split("\n"):
                lines.append(f"│ {l}")
        lines.append("└──────────────────────────────────────────────")
        lines.append("")

    # ── 第3层: 离散事件 (只展示触发时刻之前、且在最近30分钟内的) ──
    recent_events = []
    for e in flash_crashes:
        if e["idx"] <= i and e["idx"] > i - 180:  # 最近3小时内
            recent_events.append(e)
    for e in vol_spikes:
        if e["idx"] <= i and e["idx"] > i - 180:
            recent_events.append(e)
    for e in bounces:
        if e["idx"] <= i and i - e["idx"] < 30:
            recent_events.append(e)

    if recent_events:
        lines.append("┌─ 近期事件 ───────────────────────────────────")
        for e in sorted(recent_events, key=lambda x: x["idx"]):
            lines.append(f"│ {e['type']}  [{e['window']}]")
            lines.append(f"│   {e['detail']}")
        lines.append("└──────────────────────────────────────────────")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════

print("=" * 70)
print(f"  {SYM}  2026-06-16  理想邮件输出")
print("  三层告警: 日内趋势 | 持续状态 | 离散事件")
print("=" * 70)

# 检测所有事件
flash_crashes = find_flash_crashes()
vol_spikes = find_volume_spikes()
bounces = find_bounce_segments()
states = track_states()

print(f"\n📋 事件检测汇总:")
print(f"  闪电崩盘: {len(flash_crashes)} 次")
for e in flash_crashes:
    print(f"    {e['time']} | {e['window']} | {e['detail']}")

print(f"  放量异常: {len(vol_spikes)} 次")
for e in vol_spikes:
    print(f"    {e['time']} | {e['detail']}")

print(f"  低位反弹: {len(bounces)} 次")
for e in bounces:
    print(f"    {e['time']} | {e['window']} | {e['detail']}")

print(f"  状态变化: {len(states)} 次")
for s in states:
    print(f"    {s['state']}: {s['start']} → {s['end']} | {s['detail']}")

# 决定邮件
triggers = decide_emails(flash_crashes, vol_spikes, bounces, states)

print(f"\n{'='*70}")
print(f"  📧 全天邮件 ({len(triggers)} 封)")
print(f"{'='*70}")

for i, (minute_idx, reason, priority) in enumerate(triggers, 1):
    content = build_email_content(minute_idx)
    icon = "🔴" if priority == "instant" else "🔵"
    print(f"\n{'─'*60}")
    print(f"  {icon} 第{i}封 | 触发: {reason}")
    print(f"{'─'*60}")
    print(content)

# 对比
print(f"\n{'='*70}")
print(f"  对比")
print(f"{'='*70}")
print(f"  原始 monitor_daemon.py: 74 封邮件, 195 行 MRVL 相关内容")
print(f"  新三层框架:          {len(triggers)} 封邮件, 每封 MRVL 信息不超过 15 行")
print(f"  告警密度: 离散事件 {len(flash_crashes)+len(vol_spikes)+len(bounces)} 条 →")
print(f"            邮件只展示最近相关的, 不逐分钟重复")
