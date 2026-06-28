#!/usr/bin/env python3
"""
精确模拟 monitor_daemon.py 邮件系统
输入: 6月16日 alert 数据
输出: 按真实逻辑，会发几封邮件，每封内容是什么
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from debug.email_duplication_bug.analyze_warnings import load_day_data, detect_and_trade, ALERT_STYLES

SYMBOLS = ["SOXL", "KORU", "MRVL", "AAOX"]
CHECK_INTERVAL = 5        # 分钟 (monitor 默认 300秒)
COOLDOWN_SECONDS = 600    # 10分钟冷却


# ═══════════════════════════════════
# 与 monitor_daemon.py 完全一致的 3 个函数
# ═══════════════════════════════════

def _parse_alert_line(line: str) -> tuple[str, str, str]:
    """第753行 — 从告警行提取 (symbol, alert_type, severity)"""
    sym = ""; alert_type = ""; severity = "warning"
    for word in line.replace(":","").replace("⚠️","").replace("🚫","").replace("🎯","").replace("📌","").replace("🟢","").replace("🟡","").replace("🔴","").replace("🔔","").split():
        w = word.strip("$").strip(".")
        if w.isupper() and 2 <= len(w) <= 5 and w not in ("STRONG","MEDIUM","WEAK","HIGH","LOW","VWAP","SMA"):
            sym = w; break
    if "跌破" in line or "止损" in line:         alert_type, severity = "stop_loss", "critical"
    elif "止盈" in line:                          alert_type, severity = "take_profit", "critical"
    elif "闪电崩盘" in line:                      alert_type, severity = "flash_crash", "critical"
    elif "暴涨" in line or "急涨" in line:        alert_type, severity = "sharp_rise", "critical"
    elif "急跌" in line:                          alert_type, severity = "sharp_drop", "critical"
    elif "VWAP" in line:                          alert_type, severity = "vwap_deviation", "warning"
    elif "成交量" in line or "量比" in line:      alert_type, severity = "volume_spike", "warning"
    elif "反弹" in line:                          alert_type, severity = "bounce", "info"
    elif "新低" in line:                          alert_type, severity = "near_low", "warning"
    elif "突破" in line:                          alert_type, severity = "breakout", "info"
    return sym, alert_type, severity


def _resolve_alert_type(line: str) -> str:
    """第704-719行 — 回退告警类型解析"""
    if "急跌" in line:        return "sharp_drop"
    elif "急涨" in line:      return "sharp_rise"
    elif "反弹" in line:      return "bounce"
    elif "闪电" in line:      return "flash_crash"
    elif "VWAP" in line:      return "vwap_deviation"
    elif "成交量" in line or "量比" in line: return "volume_spike"
    elif "新低" in line:      return "near_low"
    elif "止盈" in line:      return "take_profit"
    elif "止损" in line:      return "stop_loss"
    elif "突破" in line:      return "breakout"
    else:                     return "watchlist_move"


# ═══════════════════════════════════
# 核心模拟
# ═══════════════════════════════════

def simulate(all_alerts: dict[str, list[dict]]):
    """
    模拟 monitor_daemon.py 一天运行。
    每 CHECK_INTERVAL 分钟检查一次, 收集新告警 →
    _update_active_alerts → _should_send_alert → 组装邮件
    """

    # 把告警展开为 (minute_idx, symbol, alert_dict)
    flat = []
    for sym, alerts in all_alerts.items():
        for a in alerts:
            h, m = map(int, a["time"].split(":"))
            mi = (h - 9) * 60 + (m - 30)
            flat.append((max(mi, 0), sym, a))
    flat.sort(key=lambda x: (x[0], x[1]))

    total_min = 390
    check_times = list(range(25, total_min + 1, CHECK_INTERVAL))
    base = datetime(2026, 6, 16, 9, 30)

    active_alerts: dict[str, str] = {}    # key=SYM|type → text
    alert_cooldown: dict[str, tuple[float, str]] = {}  # key → (ts, severity)
    emails_sent = []

    for check_min in check_times:
        check_dt = base + timedelta(minutes=check_min)
        check_ts = check_dt.timestamp()
        tlabel = check_dt.strftime("%H:%M")

        # 本窗口新告警
        ws = check_min - CHECK_INTERVAL
        new = [(sym, a) for m, sym, a in flat if ws < m <= check_min]
        if not new:
            continue

        # ── email_parts (与 monitor_daemon 完全一致) ──
        email_parts = []
        for sym, a in new:
            email_parts.append(
                f"- {sym}: {a['reason']} | ${a['price']:.2f} | {a['time']}"
            )

        # ── _update_active_alerts (第693行) ──
        for line in email_parts:
            sym, atype, _ = _parse_alert_line(line)
            if not sym: continue
            if not atype: atype = _resolve_alert_type(line)
            active_alerts[f"{sym}|{atype}"] = line.strip("- ")

        current_keys = set()
        for line in email_parts:
            sym, atype, _ = _parse_alert_line(line)
            if not sym: atype = _resolve_alert_type(line); continue
            if not atype: atype = _resolve_alert_type(line)
            current_keys.add(f"{sym}|{atype}")
        stale = [k for k in active_alerts if k not in current_keys and "sharp" not in k]
        for k in stale:
            del active_alerts[k]

        # ── _should_send_alert (第787行) ──
        should_send = False
        for line in email_parts:
            sym, atype, severity = _parse_alert_line(line)
            if not sym: continue
            if not atype: atype = _resolve_alert_type(line)
            key = f"{sym}|{atype}"
            last_ts, last_sev = alert_cooldown.get(key, (0, ""))

            if severity == "critical" and last_sev == "warning":
                pass  # 升级，冷却作废
            elif check_ts - last_ts < COOLDOWN_SECONDS:
                continue  # 冷却中，跳过

            should_send = True
            alert_cooldown[key] = (check_ts, severity)

        if not should_send:
            continue

        # ── 邮件组装 (第501-510行) ──
        all_parts = list(email_parts)
        seen = set()
        for p in all_parts:
            t = p.strip("-⚠️🚫🎯📌🔔🔴🟡🟢📉📈➡️⚡💡*📊📈 VWAP").strip()
            if len(t) > 10: seen.add(t[:60])

        added = []
        for ak, atxt in active_alerts.items():
            d = atxt.strip("-⚠️🚫🎯📌🔔🔴🟡🟢📉📈➡️⚡💡*📊📈 VWAP")
            if d[:60] not in seen:
                all_parts.append(f"- {atxt}")
                added.append(atxt)
                seen.add(d[:60])

        # 涉及股票
        syms_in = sorted(set(
            s for p in all_parts for s in SYMBOLS if s in p
        ))
        has_crit = any("急跌" in p or "闪" in p or "止损" in p for p in all_parts)

        emails_sent.append({
            "time": tlabel, "check_min": check_min,
            "subject": "🚨 IBKR 监控告警" if has_crit else "📊 IBKR 监控报告",
            "n_new": len(email_parts), "n_added": len(added),
            "n_total": len(all_parts), "syms": syms_in,
            "pool_size": len(active_alerts),
            "lines": all_parts,
        })

    return emails_sent, active_alerts, alert_cooldown


# ═══════════════════════════════════
# 输出
# ═══════════════════════════════════

def main():
    print("=" * 70)
    print("monitor_daemon.py 邮件系统 — 精确模拟")
    print(f"股票: {', '.join(SYMBOLS)}  |  间隔{CHECK_INTERVAL}分  |  冷却{COOLDOWN_SECONDS}秒")
    print("=" * 70)

    all_alerts = {}
    for sym in SYMBOLS:
        df = load_day_data(sym)
        if df is None: continue
        alerts, _ = detect_and_trade(sym, df)
        all_alerts[sym] = alerts

    emails, active, cooldown = simulate(all_alerts)

    # ── 逐封展示 ──
    for i, em in enumerate(emails, 1):
        print(f"\n{'─'*65}")
        print(f"📧 第{i}封  |  {em['time']}  |  {em['subject']}")
        print(f"   新告警{em['n_new']}条 + 活跃池补{em['n_added']}条 = 共{em['n_total']}条")
        print(f"   股票: {', '.join(em['syms'])}  |  活跃池: {em['pool_size']}条")
        print(f"{'─'*65}")

        # 按股票分组
        by_sym = defaultdict(list)
        for l in em["lines"]:
            for s in SYMBOLS:
                if s in l: by_sym[s].append(l.strip("- ")); break
            else: by_sym["其他"].append(l)
        for s in sorted(by_sym):
            print(f"  [{s}] ({len(by_sym[s])}条)")
            for l in by_sym[s]:
                print(f"    {l}")

    # ── 总结 ──
    print(f"\n{'='*70}")
    print(f"📊 总结")
    print(f"{'='*70}")
    print(f"  全天: {len(emails)} 封邮件")

    total_lines = sum(e["n_total"] for e in emails)
    print(f"  总告警行: {total_lines} (原始{sum(len(v) for v in all_alerts.values())}条)")

    # 重复分析
    all_lines = [l for e in emails for l in e["lines"]]
    dupes = len(all_lines) - len(set(all_lines))
    print(f"  完全重复行: {dupes}")

    # 同股票+类型跨邮件出现
    st = defaultdict(list)
    for i, e in enumerate(emails):
        for l in e["lines"]:
            sym, atype, _ = _parse_alert_line(l)
            if not sym: continue
            if not atype: atype = _resolve_alert_type(l)
            st[f"{sym}|{atype}"].append(i+1)
    print(f"\n  同股票+同类型跨邮件重复:")
    multi = {k: v for k, v in st.items() if len(v) > 1}
    for k, vs in sorted(multi.items(), key=lambda x: -len(x[1])):
        print(f"    {k}: 出现在邮件 {vs}")

    # 股票覆盖
    print(f"\n  各股票覆盖:")
    for s in SYMBOLS:
        mail_ids = [i+1 for i, e in enumerate(emails) if s in e["syms"]]
        total_s = len(all_alerts.get(s, []))
        lines_s = sum(1 for e in emails for l in e["lines"] if s in l)
        print(f"    {s}: 检测{total_s}条告警 → 邮件{lines_s}行 → 出现在邮件 {mail_ids}")

    # Bug 确认
    print(f"\n{'─'*65}")
    print("🐛 Bug 确认:")
    print(f"  1) 查看上面 '活跃池补' > 0 的邮件 → 活跃池内容混入")
    print(f"  2) 同股票同类型多次出现在不同邮件 → 重复问题")
    print(f"  3) 邮件中同股票内多行同一类型告警 → 60字符去重失败")


if __name__ == "__main__":
    main()
