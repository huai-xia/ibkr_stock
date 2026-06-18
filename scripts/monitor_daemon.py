#!/usr/bin/env python3
"""
后台持仓监控守护进程
不需要 Claude Code，终端里直接跑

用法:
    # 启动（默认每5分钟检查一次）
    python3 scripts/monitor_daemon.py

    # 自定义间隔（秒）
    python3 scripts/monitor_daemon.py --interval 180

    # 仅检查不推送
    python3 scripts/monitor_daemon.py --no-push

    # 停止: Ctrl+C
"""

import sys
import time
import signal
import argparse
from pathlib import Path
from datetime import datetime

# 项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.connection import ConnectionManager
from src.analysis.monitor import PositionMonitor, PositionAlert
from src.analysis.exit_strategy import ExitStrategyEngine
from src.analysis.stock_data import StockDataManager
from src.data.price_validator import PriceValidator
from src.data.minute_store import MinuteStore, collect_extended_snapshots
from src.data.feature_cache import FeatureCache
from src.analysis.anomaly import AnomalyDetector
from src.notify.email import EmailNotifier
from src.trade.portfolio import Portfolio
from src.analysis.signals import SignalDetector
from src.strategy.indicators import add_all
from src.config import get_env, FINNHUB_API_KEY
import yaml

# ── 状态文件 ──
STATUS_FILE = Path("data/monitor_status.txt")
ALERTS_FILE = Path("data/alerts_today.md")
STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)

running = True


def signal_handler(sig, frame):
    global running
    running = False
    print("\n  👋 收到停止信号，正在退出...")


def log(msg: str):
    """记录日志"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(STATUS_FILE, "a") as f:
        f.write(line + "\n")


def main():
    parser = argparse.ArgumentParser(description="IBKR 后台持仓监控")
    parser.add_argument("--interval", type=int, default=300, help="检查间隔秒数 (默认300=5分钟)")
    parser.add_argument("--port", type=int, default=4002, help="IBKR 端口")
    parser.add_argument("--no-push", action="store_true", help="不推送邮件")
    parser.add_argument("--risk", default="moderate", help="风险偏好")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 清空状态文件
    with open(STATUS_FILE, "w") as f:
        f.write("")

    # 初始化/续写当日告警文件
    today_str = datetime.now().strftime("%Y-%m-%d")
    if ALERTS_FILE.exists():
        existing = ALERTS_FILE.read_text()
        if today_str not in existing:
            # 新的一天，清空
            ALERTS_FILE.write_text(f"# 📊 当日告警记录 — {today_str}\n\n")
    else:
        ALERTS_FILE.write_text(f"# 📊 当日告警记录 — {today_str}\n\n")

    # 读取自选股列表
    watchlist = _load_watchlist()
    log(f"🚀 持仓监控启动 (间隔 {args.interval}秒, 端口 {args.port})")
    log(f"   持仓: (从IBKR获取) | 自选: {len(watchlist)}只")
    log(f"   Ctrl+C 停止")

    # 重置告警冷却（每次启动清零）
    global _alert_cooldown, _active_alerts
    _alert_cooldown.clear()
    _active_alerts.clear()

    # 连接状态追踪
    ibkr_was_down = False
    futu_was_down = False

    iteration = 0

    while running:
        iteration += 1
        log(f"🔍 第 {iteration} 次检查")

        ib = None
        try:
            # ── 连接健康检查 ──
            # 1. IBKR
            ibkr_ok = _check_ibkr(args.host, args.port)
            if not ibkr_ok:
                if not ibkr_was_down:
                    log("  ❌ IBKR Gateway 连接失败！")
                    email_parts.append("- ❌ IBKR Gateway 连接失败，无法获取持仓和行情数据")
                    ibkr_was_down = True
            elif ibkr_was_down:
                log("  ✅ IBKR Gateway 连接恢复")
                email_parts.append("- ✅ IBKR Gateway 连接已恢复")
                ibkr_was_down = False

            # 2. 富途
            futu_ok = _check_futu()
            if not futu_ok:
                if not futu_was_down:
                    log("  ❌ 富途 OpenD 连接失败！")
                    email_parts.append("- ❌ 富途 OpenD 连接失败，夜盘/盘前数据不可用")
                    futu_was_down = True
            elif futu_was_down:
                log("  ✅ 富途 OpenD 连接恢复")
                email_parts.append("- ✅ 富途 OpenD 连接已恢复")
                futu_was_down = False
        except:
            pass

        try:
            # 连接
            cm = ConnectionManager(host="127.0.0.1", port=args.port, client_id=250, max_retries=3)
            ib = cm.connect()

            # 下载缺失的持仓数据
            pf = Portfolio(ib)
            positions = pf.get_positions()
            active = [p["symbol"] for p in positions if p.get("position", 0) != 0]

            if active:
                mgr = StockDataManager(ib)
                for sym in active:
                    if mgr.load(sym) is None:
                        log(f"  📥 首次下载 {sym} 历史数据...")
                        mgr.download_one(sym, days=365)

            # 执行监控
            monitor = PositionMonitor(ib)
            alerts = monitor.check(risk_profile=args.risk)
            snapshots = monitor.snapshot()

            # 展示持仓
            for s in snapshots:
                pnl_str = f"${s.get('pnl', 0):+.0f}" if s.get('pnl') else "N/A"
                stop_str = f"止损${s['stop']:.2f}" if s.get('stop') else ""
                target_str = f"止盈${s['target']:.2f}" if s.get('target') else ""
                extra = ""
                if stop_str or target_str:
                    extra = f" | {stop_str} | {target_str}"
                log(f"  {s['symbol']}: ${s['current']:.2f} {pnl_str}{extra}")

            # 处理告警
            # ── 策略文件检查（加减仓触发）──
            strategy_alerts = _check_strategy_triggers()
            if strategy_alerts:
                alerts.extend(strategy_alerts)
                for sa in strategy_alerts:
                    log(f"  📌 {sa.message}")

            if alerts:
                log(f"  ⚠️ {len(alerts)} 条告警!")
                for a in alerts:
                    log(f"    {a.message}")
            else:
                log(f"  ✅ 无告警")

            # ── 提前定义变量，避免 UnboundLocalError ──
            extended_move_alerts = []

            # ── 自选股短线监控（盘中实时 + 资金流向 + 新闻）──
            if watchlist:
                intraday_signals = []
                watchlist_email_alerts = []  # 收集需要推送邮件的重要信号
                news_checked = set()

                for item in watchlist:
                    sym = item["symbol"]
                    wl_stop = item.get("stop", 0)
                    wl_target = item.get("target", 0)

                    if sym in active:
                        continue  # 持仓的已在上面监控

                    try:
                        from futu import OpenQuoteContext, RET_OK
                        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
                        ret, snap = ctx.get_market_snapshot([f'US.{sym}'])
                        ctx.close()

                        if ret != RET_OK or len(snap) == 0:
                            continue

                        row = snap.iloc[0]
                        current = row.get('last_price', 0)
                        today_open = row.get('open_price', 0)
                        prev_close = row.get('prev_close_price', 0)
                        high = row.get('high_price', 0)
                        low = row.get('low_price', 0)
                        volume = row.get('volume', 0)
                        turnover = row.get('turnover', 0)
                        change_rate = row.get('change_rate', 0)
                        update_time = str(row.get('update_time', ''))

                        if current <= 0:
                            continue

                        intraday_pct = (current - today_open) / today_open * 100 if today_open > 0 else 0
                        day_change_pct = (current - prev_close) / prev_close * 100 if prev_close > 0 else 0

                        # ── 自选股止损止盈检查 ──
                        if wl_stop > 0 and current <= wl_stop:
                            msg = f"🛑 {sym} 跌破自选止损 ${current:.2f} ≤ ${wl_stop:.2f}"
                            log(f"  {msg}")
                            watchlist_email_alerts.append(msg)
                        elif wl_target > 0 and current >= wl_target:
                            msg = f"🎯 {sym} 达到自选止盈 ${current:.2f} ≥ ${wl_target:.2f}"
                            log(f"  {msg}")
                            watchlist_email_alerts.append(msg)
                        elif wl_stop > 0 and (current - wl_stop) / current < 0.03:
                            msg = f"⚠️ {sym} 接近自选止损 ${wl_stop:.2f} (距 {(current-wl_stop)/current*100:.1f}%)"
                            log(f"  {msg}")
                            watchlist_email_alerts.append(msg)

                        # ── 资金流向估算 ──
                        fund_flow = _estimate_fund_flow(current, prev_close, volume)
                        is_sharp = abs(day_change_pct) > 4 or abs(intraday_pct) > 5

                        # ── 短线信号 ──
                        sigs = []

                        if intraday_pct < -3:
                            sigs.append({
                                "type": "buy", "reason": f"盘中急跌 {intraday_pct:+.1f}%（距开盘）",
                                "strength": "strong" if intraday_pct < -5 else "medium",
                                "note": "短线超跌"
                            })
                        elif intraday_pct > 5:
                            sigs.append({
                                "type": "alert", "reason": f"盘中急涨 {intraday_pct:+.1f}%（距开盘）",
                                "strength": "strong" if intraday_pct > 8 else "medium",
                                "note": "日内涨幅过大，追高风险极高"
                            })

                        if high > 0 and low > 0 and current > low * 1.02:
                            bounce_pct = (current - low) / low * 100
                            if bounce_pct > 3:
                                sigs.append({
                                    "type": "buy", "reason": f"日内低位反弹 {bounce_pct:.1f}%",
                                    "strength": "medium",
                                    "note": f"从${low:.2f}反弹，支撑确认"
                                })

                        if low > 0 and current < low * 1.01:
                            sigs.append({
                                "type": "alert", "reason": "接近日内新低", "strength": "medium",
                                "note": "可能继续下探"
                            })

                        if prev_close > 0 and today_open > prev_close * 1.02 and intraday_pct < -1:
                            sigs.append({
                                "type": "alert", "reason": "跳空高开回落，多头力竭",
                                "strength": "strong",
                                "note": f"跳空+{(today_open-prev_close)/prev_close*100:.1f}%后回落{intraday_pct:+.1f}%"
                            })

                        if day_change_pct < -5:
                            sigs.append({
                                "type": "alert", "reason": f"单日暴跌 {day_change_pct:+.1f}%",
                                "strength": "strong",
                                "note": "单日跌幅超5%，检查是否有负面新闻"
                            })

                        for s in sigs:
                            intraday_signals.append({
                                "symbol": sym, "current": current, "signal": s,
                                "intraday_pct": intraday_pct, "day_change_pct": day_change_pct,
                                "fund_flow": fund_flow, "is_sharp": is_sharp,
                                "prev_close": prev_close, "volume": volume,
                            })

                    except Exception:
                        pass

                # ── 展示信号（含资金流向 + 新闻）──
                if intraday_signals:
                    shown_symbols = set()

                    # 优先显示 sharp move 的信号
                    sharp = [s for s in intraday_signals if s["is_sharp"]]
                    others = [s for s in intraday_signals if not s["is_sharp"]]
                    display = (sharp[:5] + others)[:8]

                    for s in display:
                        sym = s["symbol"]
                        sig = s["signal"]
                        emoji = {"buy": "🟢", "sell": "🔴", "alert": "🟡"}.get(sig["type"], "📌")

                        log(f"  {emoji} [{sig['strength'].upper()}] {sym}: {sig['reason']}")
                        log(f"     ${s['current']:.2f} | 距昨收 {s['day_change_pct']:+.1f}% | "
                            f"距开盘 {s['intraday_pct']:+.1f}%")
                        log(f"     💰 {s['fund_flow']}")

                        if s["is_sharp"] and sym not in shown_symbols:
                            shown_symbols.add(sym)
                            news = _check_news_for_symbol(sym)
                            if news:
                                log(f"     📰 相关新闻: {news[0]}")
                            else:
                                log(f"     📰 暂无相关新闻 → 可能是技术面驱动或市场情绪")

                        if sig["note"]:
                            log(f"     💡 {sig['note']}")
                # ═══════════════════════════════════════════
                # MDP 数据收集 (必须在邮件组装前执行)
                # ═══════════════════════════════════════════
                # 此块从下方移动上来，确保数据在邮件组装时就绪

                # ── MDP: 分钟数据收集 + 异常检测 + 盘前异动 ──
                anomaly_alerts = []
                try:
                    detector = AnomalyDetector()

                    # 收集夜盘快照（批量，仅美股，排除韩国/加密等）
                    all_syms = list(set(
                        [p["symbol"] for p in pf.get_positions() if p.get("position", 0) != 0]
                        + [item["symbol"] for item in watchlist]
                    ))
                    # 只保留美股代码 (1-5位大写字母，不含数字和点)
                    import re
                    all_syms = [s for s in all_syms if re.match(r'^[A-Z]{1,5}$', s)]
                    if all_syms:
                        snapshots = collect_extended_snapshots(all_syms)
                        for sym, snap in snapshots.items():
                            prev_close = snap.get("prev_close", 0)
                            price = snap.get("price", 0)

                            # 盘前/夜盘显著涨跌直接进邮件
                            if prev_close > 0 and price > 0:
                                change = (price - prev_close) / prev_close * 100
                                if abs(change) > 3:
                                    direction = "📈 大涨" if change > 0 else "📉 大跌"
                                    extended_move_alerts.append(
                                        f"{direction} {sym} {change:+.1f}% "
                                        f"(昨收${prev_close:.2f} → ${price:.2f})"
                                    )

                            ms = MinuteStore(sym)
                            df_ext = ms.load_extended()
                            if df_ext is not None and not df_ext.empty:
                                # 特征缓存
                                fc = FeatureCache(sym)
                                fc.update_from_extended(df_ext)
                                # 异常检测
                                prev_close = snap.get("prev_close", 0)
                                for a in detector.detect_extended(sym, df_ext, prev_close):
                                    anomaly_alerts.append(a)

                    # 盘中数据：如果已有1分钟线，也检测
                    for sym in all_syms[:20]:  # 限制数量
                        ms = MinuteStore(sym)
                        df_1min = ms.recent_1min(30)
                        if df_1min is not None and not df_1min.empty:
                            fc = FeatureCache(sym)
                            fc.update_from_1min(df_1min)
                            for a in detector.detect_regular(sym, df_1min):
                                anomaly_alerts.append(a)

                except Exception as e:
                    log(f"  ⚠️ MDP异常: {e}")
                # ── 汇总所有告警（含持仓+自选），统一发一封邮件 ──
                if not args.no_push:
                    email_parts = []
                    friend_parts = []  # 朋友只看自选股
                    advice_parts = []  # 操作建议

                    # ① 持仓告警 + 操作建议
                    if alerts:
                        for a in alerts:
                            msg_line = f"- {a.message}"
                            email_parts.append(msg_line)
                            # 朋友不收持仓告警，跳过
                            advice = _get_advice(a)
                            if advice:
                                advice_parts.append(advice)
                        email_parts.append("")

                    # ② 自选股异动（去重：每只股票只保留最重要的1条信号）
                    seen_syms = set()
                    deduped_signals = []
                    for s in intraday_signals:
                        sym = s["symbol"]
                        if sym in seen_syms:
                            continue
                        # 只要有显著异动（>3% 日内或日间）就告警
                        if abs(s.get("day_change_pct", 0)) > 3 or abs(s.get("intraday_pct", 0)) > 4:
                            seen_syms.add(sym)
                            deduped_signals.append(s)

                    # ③ 自选股止损止盈
                    wl_alerts_deduped = list(dict.fromkeys(watchlist_email_alerts))

                    # ④ 盘前/夜盘异动
                    if extended_move_alerts:
                        for alert in extended_move_alerts[:10]:
                            line = f"- {alert}"
                            email_parts.append(line)
                            friend_parts.append(line)  # 朋友也看
                            if "大涨" in alert:
                                advice_parts.append(f"⚠️ {alert.split()[1]}: 盘前大涨，不宜追高，等开盘确认")
                            elif "大跌" in alert:
                                advice_parts.append(f"👀 {alert.split()[1]}: 盘前大跌，关注是否有负面新闻")

                    if deduped_signals or wl_alerts_deduped:
                        for s in deduped_signals:
                            line = (f"- {s['symbol']}: {s['signal']['reason']} | "
                                    f"${s['current']:.2f} | 距昨收 {s['day_change_pct']:+.1f}% | "
                                    f"{s['fund_flow']}")
                            email_parts.append(line)
                            friend_parts.append(line)  # 朋友也看
                        for a in wl_alerts_deduped:
                            email_parts.append(f"- {a}")
                            friend_parts.append(f"- {a}")
                        email_parts.append("")

                    if email_parts:
                        # ── 更新活跃告警池 + 冷却检查 ──
                        _update_active_alerts(email_parts)

                        if iteration == 1 or _should_send_alert(email_parts):  # 首轮无条件发送
                            # 组装邮件：去重合并
                            all_parts = list(email_parts)
                            seen_texts = set()
                            for p in all_parts:
                                # 用告警核心文本做去重键（去掉前缀符号）
                                text = p.strip("-⚠️🚫🎯📌🔔🔴🟡🟢📉📈➡️⚡💡* ").strip()
                                if len(text) > 10:
                                    seen_texts.add(text[:60])
                            for alert_key, alert_text in _active_alerts.items():
                                dedup_text = alert_text.strip("-⚠️🚫🎯📌🔔🔴🟡🟢📉📈➡️⚡💡* ")
                                if dedup_text[:60] not in seen_texts:
                                    all_parts.append(f"- {alert_text}")
                                    seen_texts.add(dedup_text[:60])

                            # 操作建议
                            if advice_parts:
                                all_parts.append("## 💡 操作建议\n")
                                for adv in advice_parts[:5]:
                                    all_parts.append(f"- {adv}")

                            # ── 写入当日告警文件 ──
                            _write_alerts_file(all_parts)

                            email_body = _build_html_email(all_parts)

                            has_critical = any(
                                "跌破" in p or "🚫" in p for p in all_parts
                            )
                            subject = "🚨 IBKR 监控告警" if has_critical else "📊 IBKR 监控报告"
                            _send_email(subject, email_body)
                            log(f"  📧 监控报告已推送 (活跃告警: {len(_active_alerts)}条)")

                            # 给朋友单独发（仅自选股，不含持仓）
                            if friend_parts and len(friend_parts) > 2:
                                _send_friend_email(friend_parts)
                                log(f"  📧 朋友推送已发送 ({len(friend_parts)}条自选异动)")
                        else:
                            log(f"  ⏳ 告警冷却中 (活跃: {len(_active_alerts)}条)，跳过推送")


                # ── L1 多框架共振 ──
                resonance_alerts = []
                try:
                    for sym in all_syms[:10]:  # 聚合计算较重，限制数量
                        ms = MinuteStore(sym)
                        agg = ms.aggregate_all()
                        if len(agg) >= 2:  # 至少需要2个框架
                            ra = detector.detect_resonance(sym, agg)
                            if ra:
                                resonance_alerts.extend(ra)
                                ms.save_aggregated()  # 保存聚合结果
                except Exception as e:
                    log(f"  ⚠️ 共振异常: {e}")

                if resonance_alerts:
                    for a in resonance_alerts[:3]:
                        log(f"  {a.reason}")
                    for a in resonance_alerts:
                        email_parts.append(f"- {a.reason}")
                    anomaly_alerts.extend(resonance_alerts)

                if anomaly_alerts:
                    critical = [a for a in anomaly_alerts if a.level == "critical"]
                    warnings = [a for a in anomaly_alerts if a.level == "warning"]
                    for a in (critical[:3] + warnings[:3]):
                        log(f"  {a.reason}")
                    for a in anomaly_alerts:
                        email_parts.append(f"- {a.reason}")
                else:
                    log(f"  📡 自选股: 短线无异常信号")

            ib.disconnect()

        except ConnectionError as e:
            log(f"  ⚠️ IBKR 连接失败: {e}")
        except Exception as e:
            log(f"  ❌ 检查异常: {e}")
        finally:
            if ib and ib.isConnected():
                try:
                    ib.disconnect()
                except:
                    pass

        if not running:
            break

        # 等待下一次
        log(f"  ⏰ 下次检查: {args.interval}秒后...")
        for _ in range(min(args.interval, 10)):
            if not running:
                break
            time.sleep(1)

    log("👋 监控已停止")


def _load_watchlist() -> list[dict]:
    """从配置文件读取自选股列表（含止损止盈）"""
    watchlist_path = Path("config/watchlist.yaml")
    if not watchlist_path.exists():
        # 兜底：如果是克隆的仓库，用示例文件
        example_path = Path("config/watchlist.example.yaml")
        if example_path.exists():
            watchlist_path = example_path
        else:
            return []

    try:
        with open(watchlist_path) as f:
            content = f.read()

        items = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("─") or line.startswith("="):
                continue
            if "#" in line:
                line = line.split("#")[0].strip()

            parts = line.split()
            if not parts:
                continue

            sym = parts[0].upper()
            # 跳过分类标题（如 "软件:" "半导体:"）
            if sym.endswith(":"):
                continue
            # 跳过非股票代码（含中文或过长）
            if len(sym) > 6 or any('一' <= c <= '鿿' for c in sym):
                continue

            stop_price = float(parts[1]) if len(parts) > 1 else 0.0
            target_price = float(parts[2]) if len(parts) > 2 else 0.0

            items.append({
                "symbol": sym,
                "stop": stop_price,
                "target": target_price,
            })

        return items
    except Exception as e:
        print(f"⚠️ 读取自选股列表失败: {e}")
        return []


def _estimate_fund_flow(current: float, prev_close: float, volume: float, avg_volume: float = 0) -> str:
    """根据价格和成交量估算主力资金动向"""
    if prev_close <= 0:
        return "数据不足"

    change_pct = (current - prev_close) / prev_close * 100
    vol_ratio = volume / avg_volume if avg_volume > 0 else 1.0

    # 简化判断逻辑
    if change_pct > 2 and vol_ratio > 2:
        return "🔥 主力大幅买入 (放量上涨)"
    elif change_pct > 0.5 and vol_ratio > 1.5:
        return "📈 资金流入 (量价齐升)"
    elif change_pct > 0:
        return "📈 温和流入"
    elif change_pct < -2 and vol_ratio > 2:
        return "🔴 主力大幅卖出 (放量下跌)"
    elif change_pct < -0.5 and vol_ratio > 1.5:
        return "📉 资金流出 (放量下跌)"
    elif change_pct < 0:
        return "📉 温和流出"
    elif vol_ratio > 3:
        return "⚠️ 异常放量，方向不明，关注"
    else:
        return "➡️ 资金平稳"


def _check_news_for_symbol(symbol: str) -> list[str]:
    """为单只股票查找相关新闻标题"""
    try:
        from src.news.fetcher import NewsFetcher
        fetcher = NewsFetcher()
        articles = fetcher.fetch_all(max_per_source=3)
        related = []
        sym_lower = symbol.lower()
        for a in articles:
            if sym_lower in a["title"].lower() or sym_lower in a.get("summary", "").lower():
                related.append(a["title"][:100])
                if len(related) >= 3:
                    break
        return related
    except:
        return []


# ── 告警冷却 + 活跃告警跟踪 ──
_alert_cooldown: dict = {}  # key: "symbol|alert_type" → (last_send_timestamp, severity)
_active_alerts: dict = {}   # 当前活跃的所有告警（所有检查周期累积）
COOLDOWN_SECONDS = 600  # 10 分钟冷却


def _update_active_alerts(email_parts: list[str]):
    """更新活跃告警池：本次触发的加入，本次未出现的清理"""
    global _active_alerts

    # 将本次的告警加入活跃池
    for line in email_parts:
        sym, alert_type, _ = _parse_alert_line(line)
        if sym and alert_type:
            key = f"{sym}|{alert_type}"
            _active_alerts[key] = line.strip("- ")

    # 清理已消失的告警（本次email_parts里没有=告警已解除）
    current_keys = set()
    for line in email_parts:
        sym, alert_type, _ = _parse_alert_line(line)
        if sym and alert_type:
            current_keys.add(f"{sym}|{alert_type}")
    # 只清理持仓相关的（自选股异动每次都变）
    stale = [k for k in _active_alerts if k not in current_keys and "sharp" not in k]
    for k in stale:
        del _active_alerts[k]


def _parse_alert_line(line: str) -> tuple[str, str, str]:
    """从告警行提取 (symbol, alert_type, severity)"""
    sym = ""
    alert_type = ""
    severity = "warning"

    for word in line.replace(":", "").replace("⚠️", "").replace("🚫", "").replace("🎯", "").replace("📌", "").replace("🟢", "").replace("🟡", "").replace("🔴", "").replace("🔔", "").split():
        w = word.strip("$").strip(".")
        if w.isupper() and 2 <= len(w) <= 5 and w not in ("STRONG", "MEDIUM", "WEAK", "HIGH", "LOW"):
            sym = w
            break

    if "跌破" in line or "🚫" in line or "触发止损" in line:
        alert_type, severity = "stop_loss", "critical"
    elif "接近止损" in line or "距离止损" in line:
        alert_type, severity = "stop_loss", "warning"
    elif "达到" in line and "止盈" in line:
        alert_type, severity = "take_profit", "critical"
    elif "接近止盈" in line or "距离止盈" in line or "距目标" in line:
        alert_type, severity = "take_profit", "warning"
    elif "加仓价" in line or "触及加仓" in line:
        alert_type, severity = "add_position", "critical"
    elif "接近加仓" in line:
        alert_type, severity = "add_position", "warning"
    elif "减仓价" in line or "触及减仓" in line:
        alert_type, severity = "reduce_position", "critical"
    elif "暴跌" in line or "急跌" in line:
        alert_type, severity = "sharp_drop", "critical"
    elif "急涨" in line or "暴涨" in line:
        alert_type, severity = "sharp_rise", "critical"

    return sym, alert_type, severity


def _should_send_alert(email_parts: list[str]) -> bool:
    """检查是否应该发送告警（冷却期外/级别升级时立即发送）"""
    global _alert_cooldown
    now = datetime.now().timestamp()

    for line in email_parts:
        sym, alert_type, severity = _parse_alert_line(line)
        if not sym or not alert_type:
            continue

        key = f"{sym}|{alert_type}"
        last_sent, last_severity = _alert_cooldown.get(key, (0, ""))

        if severity == "critical" and last_severity == "warning":
            pass  # 级别升级，冷却作废
        elif now - last_sent < COOLDOWN_SECONDS:
            return False

        _alert_cooldown[key] = (now, severity)

    return True


def _write_alerts_file(all_parts: list[str]):
    """实时更新当日告警文件"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%H:%M:%S")

    content = f"# 📊 当日告警记录 — {today_str}\n\n"
    content += f"🕐 最后更新: {ts}\n\n"
    content += f"📋 当前活跃告警: {len(_active_alerts)} 条\n\n---\n\n"

    # 活跃告警
    if _active_alerts:
        content += "## ⚠️ 当前活跃\n\n"
        for key, text in _active_alerts.items():
            content += f"- `{ts}` {text}\n"
    else:
        content += "## ✅ 当前无活跃告警\n"

    content += f"\n---\n*文件自动更新，监控守护进程每{COOLDOWN_SECONDS//60}分钟推送邮件*\n"

    ALERTS_FILE.write_text(content)

    # 同时追加状态日志
    with open(STATUS_FILE, "a") as f:
        f.write(f"[{ts}] 告警文件已更新 ({len(_active_alerts)}条活跃)\n")


def _get_advice(alert) -> str:
    """根据告警类型生成操作建议（含具体价格）"""
    msg = alert.message
    sym = alert.symbol

    # 止损相关
    if "跌破" in msg or "触发止损" in msg:
        return f"🔴 {sym}: 已触发止损 ${alert.threshold_price:.2f}，建议立即卖出平仓"
    if "接近止损" in msg or "距离止损" in msg:
        return f"🟡 {sym}: 距止损仅 {alert.distance_pct:.1f}%，建议设好卖单 ${alert.threshold_price:.2f}"

    # 止盈相关
    if "达到" in msg and "止盈" in msg:
        return f"🟢 {sym}: 已达止盈 ${alert.threshold_price:.2f}，建议至少卖出50%锁定利润"
    if "接近止盈" in msg or "距目标" in msg:
        return f"🟢 {sym}: 接近止盈位，可设限价卖单 ${alert.threshold_price:.2f}"

    # 加减仓
    if "触及加仓价" in msg or "加仓价" in msg:
        return f"📌 {sym}: 触及加仓线 ${alert.threshold_price:.2f}，可分批买入，注意仓位"

    # 闪电崩盘 → 给出抄底价格
    if "闪电崩盘" in msg or "急跌" in msg:
        entry, stop, target = _calc_dip_levels(sym, alert.current_price)
        if entry > 0:
            return (f"👀 {sym}: 短线超跌至 ${alert.current_price:.2f}，"
                    f"入场 ${entry:.2f} | 止损 ${stop:.2f} | 止盈 ${target:.2f}")
        return f"👀 {sym}: 短线超跌，若无利空可考虑轻仓试探"

    # VWAP偏离 > 3% → 回归目标
    if "VWAP" in msg:
        entry, stop, target = _calc_dip_levels(sym, alert.current_price)
        if target > alert.current_price:
            return (f"⚠️ {sym}: VWAP偏离，均值回归目标 ${target:.2f}，"
                    f"现价 ${alert.current_price:.2f}，距目标 {(target-alert.current_price)/alert.current_price*100:.1f}%")

    # 短线急涨
    if "急涨" in msg:
        return f"⚠️ {sym}: 短线急涨，不建议追高，已有持仓可考虑减仓"

    return ""


def _calc_dip_levels(symbol: str, current_price: float) -> tuple[float, float, float]:
    """
    计算抄底价格三要素: (入场价, 止损价, 止盈目标)
    基于 VWAP + ATR
    """
    try:
        from src.data.minute_store import MinuteStore
        from src.data.feature_cache import FeatureCache
        import numpy as np

        ms = MinuteStore(symbol)
        df = ms.load_1min()
        if df is None or df.empty:
            return (0, 0, 0)

        close = df['close'].values.astype(float)

        # SMA20作为止盈目标（近期均值回归，比VWAP更敏感）
        sma20 = float(np.mean(close[-20:])) if len(close) >= 20 else current_price

        # ATR(14) 作为止损距离
        if len(close) >= 15:
            tr_list = []
            for i in range(-14, 0):
                h = float(df.iloc[i]['high']) if 'high' in df.columns else close[i]
                l = float(df.iloc[i]['low']) if 'low' in df.columns else close[i]
                prev_c = close[i-1]
                tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                tr_list.append(tr)
            atr = float(np.mean(tr_list))
        else:
            atr = current_price * 0.03

        # 入场: 当前价（左侧抄底）
        entry = round(current_price, 2)
        # 止损: 入场价 - 2.0×ATR（给足够波动空间）
        stop = round(current_price - 2.0 * atr, 2)
        # 止盈: SMA20 + 0.5%缓冲
        target = round(sma20 * 1.005, 2)

        return (entry, stop, target)
    except:
        return (0, 0, 0)


def _build_html_email(all_parts: list[str]) -> str:
    """构建简洁HTML邮件（无Markdown标记）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 分离持仓告警、操作建议、自选异动
    position_lines = []
    watchlist_lines = []
    advice_lines = []
    in_advice = False

    for line in all_parts:
        stripped = line.strip("- ")
        if "操作建议" in stripped:
            in_advice = True
            continue
        if in_advice:
            advice_lines.append(stripped)
        elif any(kw in stripped for kw in ["止损", "止盈", "加仓", "减仓", "UNM", "MRAAY"]):
            position_lines.append(stripped)
        elif "IBKR Gateway" in stripped or "富途" in stripped:
            position_lines.append(stripped)
        else:
            watchlist_lines.append(stripped)

    html = f"""<html><body style="font-family: -apple-system, Arial, sans-serif; max-width: 600px;">
<p style="color: #666; font-size: 12px;">IBKR 监控报告 · {now} (美东)</p>
"""

    # 操作建议（置顶，最重要）
    if advice_lines:
        html += '<div style="background: #fff9e6; border-left: 4px solid #f39c12; padding: 14px; margin: 8px 0;">'
        html += '<p style="font-weight: bold; color: #e67e22; margin: 0 0 8px 0; font-size: 15px;">💡 操作建议</p>'
        for line in advice_lines:
            html += f'<p style="margin: 4px 0; font-size: 14px; font-weight: 500;">{line}</p>'
        html += '</div>'

    if position_lines:
        html += '<div style="background: #fff3f0; border-left: 3px solid #e74c3c; padding: 12px; margin: 8px 0;">'
        html += '<p style="font-weight: bold; color: #c0392b; margin: 0 0 8px 0;">🛡️ 持仓告警</p>'
        for line in position_lines:
            html += f'<p style="margin: 4px 0; font-size: 14px;">{line}</p>'
        html += '</div>'

    if watchlist_lines:
        html += '<div style="background: #f8f9fa; border-left: 3px solid #3498db; padding: 12px; margin: 8px 0;">'
        html += '<p style="font-weight: bold; color: #2980b9; margin: 0 0 8px 0;">📡 自选股异动</p>'
        for line in watchlist_lines[:8]:
            html += f'<p style="margin: 4px 0; font-size: 14px;">{line}</p>'
        html += '</div>'

    html += '<p style="color: #aaa; font-size: 11px; margin-top: 16px;">🤖 监控守护进程自动推送</p>'
    html += '</body></html>'

    return html


def _check_ibkr(host: str, port: int) -> bool:
    """检查 IBKR Gateway 是否在线"""
    try:
        from src.core.connection import ConnectionManager
        cm = ConnectionManager(host=host, port=port, client_id=250, max_retries=1)
        ib = cm.connect()
        ib.disconnect()
        return True
    except:
        return False


def _check_futu() -> bool:
    """检查富途 OpenD 是否在线"""
    try:
        from futu import OpenQuoteContext, RET_OK
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        ret, _ = ctx.get_global_state()
        ctx.close()
        return ret == RET_OK
    except:
        return False


def _send_friend_email(parts: list[str]) -> bool:
    """发送仅含自选股异动的邮件给朋友（无持仓信息）"""
    try:
        user = get_env("FRIEND_SMTP_USER", "")
        password = get_env("FRIEND_SMTP_PASSWORD", "")
        if not user or not password:
            return False

        body = "<html><body style='font-family:sans-serif;max-width:500px'>"
        body += f"<p>📡 自选股异动 · {datetime.now().strftime('%H:%M')}</p>"
        body += "<div style='background:#f0f4ff;padding:12px;border-left:3px solid #3498db'>"
        for p in parts[:10]:
            body += f"<p style='margin:4px 0;font-size:14px'>{p.strip('- ')}</p>"
        body += "</div></body></html>"

        notifier = EmailNotifier("smtp.qq.com", 587, user, password)
        return notifier.send("📡 自选股异动", body, html=True)
    except:
        return False


def _send_email(subject: str, body: str) -> bool:
    """发送告警邮件"""
    try:
        from src.notify.email import EmailNotifier
        from src.config import get_env
        smtp_user = get_env("SMTP_USER", "")
        smtp_password = get_env("SMTP_PASSWORD", "")
        if not smtp_user or not smtp_password:
            return False
        notifier = EmailNotifier(
            smtp_host=get_env("SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(get_env("SMTP_PORT", "587")),
            user=smtp_user, password=smtp_password,
        )
        return notifier.send(subject, body, html=True)
    except:
        return False


def _check_strategy_triggers() -> list:
    """检查持仓策略文件中的加减仓触发条件"""
    from src.analysis.portfolio_strategy import STRATEGY_FILE
    import yaml

    if not STRATEGY_FILE.exists():
        return []

    try:
        with open(STRATEGY_FILE) as f:
            data = yaml.safe_load(f) or {}
    except:
        return []

    holdings = data.get("holdings", {})
    alerts = []

    for sym, h in holdings.items():
        current = h.get("current_price", 0)
        add_price = h.get("add_on_dip")
        reduce_price = h.get("reduce_on_rip")
        stop = h.get("stop_loss", 0)
        target = h.get("take_profit", 0)

        if current <= 0:
            continue

        # 加仓触发
        if add_price and add_price > 0 and current <= add_price:
            alerts.append(PositionAlert(
                symbol=sym, alert_type="add_position", level="info",
                current_price=current, threshold_price=add_price,
                distance_pct=round((current - add_price) / current * 100, 1),
                message=f"📌 {sym} 触及加仓价 ${add_price:.2f}！现价 ${current:.2f}",
                timestamp=datetime.now().strftime("%H:%M:%S"),
            ))
        # 接近加仓
        elif add_price and add_price > 0 and current <= add_price * 1.03:
            alerts.append(PositionAlert(
                symbol=sym, alert_type="near_add", level="info",
                current_price=current, threshold_price=add_price,
                distance_pct=round((add_price - current) / current * 100, 1),
                message=f"🔔 {sym} 接近加仓价 ${add_price:.2f}（现价 ${current:.2f}，差 {(add_price-current)/current*100:.1f}%）",
                timestamp=datetime.now().strftime("%H:%M:%S"),
            ))

        # 减仓触发
        if reduce_price and reduce_price > 0 and current >= reduce_price:
            alerts.append(PositionAlert(
                symbol=sym, alert_type="reduce_position", level="warning",
                current_price=current, threshold_price=reduce_price,
                distance_pct=round((current - reduce_price) / reduce_price * 100, 1),
                message=f"📌 {sym} 触及减仓价 ${reduce_price:.2f}！现价 ${current:.2f}",
                timestamp=datetime.now().strftime("%H:%M:%S"),
            ))

    return alerts


if __name__ == "__main__":
    main()
