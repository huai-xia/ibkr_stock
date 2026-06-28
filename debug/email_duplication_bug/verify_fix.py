#!/usr/bin/env python3
"""验证 monitor_daemon.py 修复效果 — 使用6月16号数据模拟全天"""

import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path.cwd()))

# 直接导入修复后的新函数
from scripts.monitor_daemon import (
    _check_events, _get_snapshot, _make_recommendations,
    _should_send_email, _build_email_table
)

from debug.email_duplication_bug.analyze_warnings import load_day_data

SYMBOLS = ["SOXL", "KORU", "MRVL", "AAOX", "AAPL", "AEIS", "ARM"]

OUT = Path("debug/email_duplication_bug/output/monitor_fix_verification.txt")

# 重置全局状态
import scripts.monitor_daemon as md
md._event_history.clear()
md._last_email_minute = -99

lines = []
lines.append("=" * 70)
lines.append("monitor_daemon.py 修复验证 — 2026-06-16 全天模拟")
lines.append(f"股票: {', '.join(SYMBOLS)}")
lines.append("=" * 70)

all_events_global = []
emails_sent = 0

# 预计算: 每只股票用完整数据跑一次事件检测, 模拟真实系统(数据累积)
all_events_full = {}  # sym -> list of events with absolute idx
all_snapshots_full = {}  # sym -> df
for sym in SYMBOLS:
    df = load_day_data(sym)
    if df is None: continue
    all_snapshots_full[sym] = df
    all_events_full[sym] = _check_events(sym, df)

for sim_minute in range(30, 391, 5):  # 每5分钟一次检查
    h, m = divmod(sim_minute + 570, 60)
    ts = f"{h:02d}:{m:02d}"

    stocks_data = []
    check_events = []

    for sym in SYMBOLS:
        df = all_snapshots_full.get(sym)
        if df is None: continue
        i = min(sim_minute, len(df) - 1)
        if i < 20: continue
        df_visible = df.iloc[:i+1].copy()

        snap = _get_snapshot(sym, df_visible)
        stocks_data.append(snap)

        # 从预计算的事件中, 过滤出当前可见且最近30分钟内的
        for e in all_events_full.get(sym, []):
            if e["idx"] <= sim_minute and e["idx"] > sim_minute - 30:
                check_events.append(e)
                all_events_global.append(e)

    if not stocks_data:
        continue

    # 操作建议
    recs = []
    for s in stocks_data:
        r = _make_recommendations(s["sym"], s, check_events)
        recs.extend(r)

    # 判断是否发送
    should, reason = _should_send_email(check_events, sim_minute)

    if should:
        emails_sent += 1
        body = _build_email_table(stocks_data, check_events, recs, reason)
        lines.append(f"\n{'─'*60}")
        lines.append(f"📧 第{emails_sent}封 | {ts} | {reason}")
        lines.append(f"{'─'*60}")
        lines.append(body)

# 统计
lines.append(f"\n{'='*70}")
lines.append(f"全天: {emails_sent} 封邮件")
lines.append(f"总事件: {len(all_events_global)} 条")

# 按类型 + 股票去重统计
unique_events = {}
for e in all_events_global:
    key = f"{e['sym']}|{e['type']}|{e['window']}"
    unique_events[key] = e
lines.append(f"唯一事件: {len(unique_events)} 条")
lines.append("")
lines.append("事件清单:")
for e in sorted(unique_events.values(), key=lambda x: x["idx"]):
    lines.append(f"  {e['sym']:5s} {e['time']} {e['type']} [{e['window']}] {e['detail']}")

lines.append(f"\n对比: 旧系统 ~74封 → 新系统 {emails_sent}封")
lines.append(f"减少: {(1-emails_sent/74)*100:.0f}%")

content = "\n".join(lines)
OUT.write_text(content)
print(content)
