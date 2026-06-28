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
import re
from pathlib import Path
from datetime import datetime
import numpy as np
import pandas as pd

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
from src.config import get_env, FINNHUB_API_KEY, get_account_dir
import yaml

# ── 状态文件（按账户隔离）──
_account_dir = get_account_dir()
STATUS_FILE = _account_dir / "monitor_status.txt"
ALERTS_FILE = _account_dir / "alerts_today.md"

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
    parser.add_argument("--confirm", action="store_true", help="发送前预览邮件内容，5秒倒计时可 Ctrl+C 取消")
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

        # ── 初始化本轮数据容器 ──

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

                        # 收集新闻（去重，每个 symbol 只查一次）
                        if s["is_sharp"] and sym not in shown_symbols:
                            shown_symbols.add(sym)
                            news = _check_news_for_symbol(sym)
                            if news:
                                s["news"] = news[0]
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

                # ── L1 多框架共振 (日志用) ──
                resonance_alerts = []
                try:
                    for sym in all_syms[:10]:
                        ms = MinuteStore(sym)
                        agg = ms.aggregate_all()
                        if len(agg) >= 2:
                            ra = detector.detect_resonance(sym, agg)
                            if ra:
                                resonance_alerts.extend(ra)
                                ms.save_aggregated()
                except Exception as e:
                    log(f"  ⚠️ 共振异常: {e}")

                if resonance_alerts:
                    for a in resonance_alerts[:3]:
                        log(f"  {a.reason}")
                    anomaly_alerts.extend(resonance_alerts)

                if anomaly_alerts:
                    critical = [a for a in anomaly_alerts if a.level == "critical"]
                    warnings = [a for a in anomaly_alerts if a.level == "warning"]
                    for a in (critical[:3] + warnings[:3]):
                        log(f"  {a.reason}")
                else:
                    log(f"  📡 自选股: 短线无异常信号")

            # ── end if watchlist ──

            # ═══════════════════════════════════════════════════════
            # 三层邮件系统: 行情表格 + 事件 + 操作建议
            # ═══════════════════════════════════════════════════════

            # 合并所有关注股票: 持仓 + 自选
            all_syms_unified = list(set(active + [item["symbol"] for item in watchlist]))
            # 只保留美股代码
            all_syms_unified = [s for s in all_syms_unified if re.match(r'^[A-Z]{1,5}$', s)]

            if all_syms_unified:
                # 收集快照 + 检测事件
                stocks_data = []
                all_events = []
                all_recs = []

                for sym in all_syms_unified:
                    try:
                        ms = MinuteStore(sym)
                        df_1min = ms.load_1min()
                        if df_1min is None or df_1min.empty:
                            continue
                        # 快照
                        entry = 0
                        if sym in active:
                            for p in positions:
                                if p["symbol"] == sym:
                                    entry = p.get("avg_cost", 0)
                                    break
                        snap = _get_snapshot(sym, df_1min, entry)
                        stocks_data.append(snap)

                        # 事件检测
                        events = _check_events(sym, df_1min)
                        all_events.extend(events)

                        # 操作建议
                        recent = [e for e in events if e["idx"] >= len(df_1min) - 60]
                        recs = _make_recommendations(sym, snap, recent)
                        all_recs.extend(recs)
                    except Exception:
                        pass

                if stocks_data:
                    # 展示终端
                    log(f"  📊 监测 {len(stocks_data)} 只股票")
                    for s in stocks_data:
                        entry_info = f" | 成本${s['entry']:.2f}" if s.get("entry", 0) > 0 else ""
                        state_str = ", ".join(s["states"]) if s["states"] else "正常"
                        log(f"    {s['sym']}: ${s['price']:.2f} ({s['pct']:+.1f}%) [{state_str}]{entry_info}")
                    for e in all_events[-5:]:
                        emoji = {"闪崩": "⚡", "暴涨": "⚡", "反弹": "🟢", "放量": "📊"}.get(e["type"], "")
                        log(f"    {emoji} {e['sym']} {e['type']} [{e['window']}] {e['detail']}")

                    # 判断是否发送邮件
                    now_dt = datetime.now()
                    current_minute = now_dt.hour * 60 + now_dt.minute
                    should_send, reason = _should_send_email(all_events, current_minute)

                    if should_send and not args.no_push:
                        email_body = _build_email_table(stocks_data, all_events, all_recs, reason)
                        subject = "🚨 IBKR 监控告警" if any(
                            e["type"] in ("闪崩", "暴涨") for e in all_events[-5:]
                        ) else "📊 IBKR 监控报告"

                        # 终端预览
                        log("─" * 50)
                        log(f"📧 邮件预览 | 主题: {subject} | 触发: {reason}")
                        log("─" * 50)
                        for line in email_body.split("\n"):
                            log(f"  {line}")
                        log("─" * 50)

                        # 更新告警文件
                        with open(ALERTS_FILE, "a") as af:
                            af.write(f"\n## {now_dt.strftime('%H:%M')} — {reason}\n\n")
                            af.write(email_body + "\n")

                        # 发送
                        if args.confirm:
                            for i in range(5, 0, -1):
                                log(f"  ⏳ {i}秒后发送 (Ctrl+C 取消)...")
                                time.sleep(1)
                                if not running:
                                    log("  🚫 发送已取消")
                                    break
                            else:
                                _send_email(subject, email_body)
                                log(f"  📧 监控报告已推送")
                        else:
                            _send_email(subject, email_body)
                            log(f"  📧 监控报告已推送")
                    elif should_send:
                        log(f"  📝 邮件已构建 (--no-push 模式，跳过发送): {reason}")
                    else:
                        log(f"  ⏭️ 跳过发送 (冷却中或无需更新)")

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
        for _ in range(args.interval):
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


# ── 三层告警引擎 ──
_event_history: dict = {}      # {(sym, event_type, window_start): last_trigger_minute}
_last_email_minute: int = -99  # 上次发邮件的时间(分钟)
ROUTINE_INTERVAL = 30          # 例行邮件最小间隔(分钟)
EVENT_COOLDOWN = 30            # 同事件冷却(分钟)


def _zscore(series, window: int) -> float:
    """滚动 Z-Score"""
    import numpy as np
    if len(series) < window:
        return 0.0
    w = series[-window:]
    mu, std = float(np.mean(w)), float(np.std(w))
    return float((series[-1] - mu) / std) if std > 0 else 0.0


def _calc_atr(high, low, close, period=14):
    """计算 ATR"""
    import numpy as np
    if len(close) < period + 1:
        return float(np.mean(np.array(high) - np.array(low)))
    tr = [max(float(high[j]) - float(low[j]),
              abs(float(high[j]) - float(close[j-1])),
              abs(float(low[j]) - float(close[j-1])))
          for j in range(-period, 0)]
    return float(np.mean(tr))


def _check_events(sym: str, df_1min) -> list[dict]:
    """
    基于1分钟K线检测离散事件（第3层）。
    返回: [{idx, time, type, window, detail, sym}, ...]
    """
    import numpy as np
    events = []
    if df_1min is None or len(df_1min) < 20:
        return events

    close = df_1min["close"].values.astype(float)
    high = df_1min["high"].values.astype(float)
    low = df_1min["low"].values.astype(float)
    volume = df_1min["volume"].values.astype(float) if "volume" in df_1min.columns else None
    dates = df_1min["date"].values if "date" in df_1min.columns else df_1min.index.values
    n = len(close)

    def _ts(i):
        return pd.Timestamp(dates[i]).strftime("%H:%M")

    # 闪崩/暴涨 (20分钟 Z-Score)
    i = 20
    while i < n:
        z = _zscore(close[:i+1], 20)
        if z < -3.0:
            bi, bz = i, z
            for j in range(i, min(n, i+10)):
                zj = _zscore(close[:j+1], 20)
                if zj < bz:
                    bz, bi = zj, j
            ws = max(0, bi-20)
            events.append({
                "idx": bi, "sym": sym, "time": _ts(bi),
                "type": "闪崩",
                "window": f"{_ts(ws)}-{_ts(bi)}",
                "detail": f"${close[ws]:.2f}→${close[bi]:.2f} ({(close[bi]-close[ws])/close[ws]*100:.1f}%), Z={bz:.1f}σ",
            })
            i = j + 15
        elif z > 3.0:
            ws = max(0, i-20)
            events.append({
                "idx": i, "sym": sym, "time": _ts(i),
                "type": "暴涨",
                "window": f"{_ts(ws)}-{_ts(i)}",
                "detail": f"${close[ws]:.2f}→${close[i]:.2f} (+{(close[i]-close[ws])/close[ws]*100:.1f}%), Z=+{z:.1f}σ",
            })
            i += 15
        else:
            i += 1

    # 成交量异常
    if volume is not None:
        merged_vol = []
        for i in range(10, n):
            rv = float(np.mean(volume[i-4:i+1]))
            bv = float(np.mean(volume[i-9:i-4]))
            if bv > 0 and rv / bv > 5.0:
                merged_vol.append({
                    "idx": i, "sym": sym, "time": _ts(i),
                    "type": "放量",
                    "window": f"{_ts(i-4)}-{_ts(i)}",
                    "detail": f"量比{rv/bv:.1f}x",
                })
        for e in merged_vol:
            if not events or e["idx"] - events[-1]["idx"] > 5 or events[-1].get("type") != "放量":
                events.append(e)
            elif float(e["detail"].split("x")[0][2:]) > float(events[-1]["detail"].split("x")[0][2:]):
                events[-1] = e

    # 低位反弹 (同一低点只记录最大反弹, 避免重复)
    bounce_raw = []
    used_lows = set()  # 已使用的低点索引, 防止同一低点被重复检测
    i = 30
    while i < n:
        rl = float(np.min(low[:i+1]))
        price = close[i]
        if rl > 0 and price > rl * 1.02 and (price - rl) / rl * 100 > 3.0:
            li = int(np.argmin(low[:i+1]))
            if li in used_lows:
                i += 1
                continue
            ei = i
            for j in range(i+1, min(n, i+15)):
                if close[j] > close[ei]:
                    ei = j
            bp = (close[ei] - rl) / rl * 100
            if bp >= 3.0:
                bounce_raw.append({
                    "idx": ei, "sym": sym, "time": _ts(ei),
                    "type": "反弹",
                    "window": f"{_ts(li)}-{_ts(ei)}",
                    "detail": f"${rl:.2f}→${close[ei]:.2f} (+{bp:.1f}%)",
                })
                used_lows.add(li)
            i = ei + 15
        else:
            i += 1
    # 去重: 同一起点的反弹只保留涨幅最大的
    seen_bounce_starts = set()
    for e in bounce_raw:
        st = e["window"].split("-")[0]
        sym_key = f"{e['sym']}|{st}"
        if sym_key in seen_bounce_starts:
            # 找已有的同起点反弹, 保留更大的
            for prev in events:
                if (prev.get("type") == "反弹" and prev.get("sym") == e["sym"]
                        and prev["window"].split("-")[0] == st):
                    prev_pct = float(prev["detail"].split("+")[1].split("%")[0])
                    this_pct = float(e["detail"].split("+")[1].split("%")[0])
                    if this_pct > prev_pct:
                        events.remove(prev)
                        events.append(e)
                    break
        else:
            seen_bounce_starts.add(sym_key)
            events.append(e)

    return sorted(events, key=lambda x: x["idx"])


def _get_snapshot(sym: str, df_1min, entry_price: float = 0) -> dict:
    """
    获取股票当前快照（第1层 + 第2层）。
    返回: {price, pct, day_high, day_low, amp, atr, vwap, sma20, states, entry}
    """
    import numpy as np
    close = df_1min["close"].values.astype(float)
    high = df_1min["high"].values.astype(float)
    low = df_1min["low"].values.astype(float)
    volume = df_1min["volume"].values.astype(float) if "volume" in df_1min.columns else np.ones(len(close))
    day_open = float(df_1min["open"].iloc[0])
    n = len(close)
    price = close[-1]
    pct = (price - day_open) / day_open * 100 if day_open > 0 else 0

    dh = float(np.max(high))
    dl = float(np.min(low))
    amp = (dh - dl) / day_open * 100 if day_open > 0 else 0

    # ATR & VWAP & SMA20
    atr_v = _calc_atr(high, low, close, 14) if n >= 15 else price * 0.03
    if n >= 5:
        wnd = min(n, 60)
        pw, vw = close[-wnd:], volume[-wnd:]
        vwap = float(np.average(pw, weights=vw)) if vw.sum() > 0 else price
    else:
        vwap = price
    sma20 = float(np.mean(close[max(0, n-20):]))

    # 状态判断 (第2层)
    dist_low = (price - dl) / dl * 100 if dl > 0 else 0
    states = []
    if pct < -3.0:
        states.append("下跌中")
    if dist_low < 1.0:
        states.append("近新低")
    if pct > 5.0:
        states.append("急涨")

    snap = {
        "sym": sym, "price": price, "pct": pct,
        "day_high": dh, "day_low": dl, "amp": amp,
        "atr": atr_v, "vwap": vwap, "sma20": sma20,
        "dist_low": dist_low, "states": states,
        "entry": entry_price,
    }
    if entry_price > 0:
        snap["pnl_pct"] = (price - entry_price) / entry_price * 100
    return snap


def _make_recommendations(sym: str, snap: dict, recent_events: list[dict]) -> list[dict]:
    """
    基于快照 + 近期事件生成买卖建议（入场/止损/止盈价 + 盈亏比）。
    """
    price = snap["price"]
    atr = snap["atr"]
    vwap = snap["vwap"]
    sma20 = snap["sma20"]
    pct = snap["pct"]
    dl = snap["day_low"]
    recs = []

    has_flash = any(e["sym"] == sym and e["type"] == "闪崩" for e in recent_events)
    has_bounce = any(e["sym"] == sym and e["type"] == "反弹" for e in recent_events)
    has_surge = any(e["sym"] == sym and e["type"] == "暴涨" for e in recent_events)

    # 买入: 闪崩抄底
    if has_flash:
        buy_price = round(max(dl * 1.01, vwap - 1.5 * atr), 2)
        stop_price = round(buy_price - 2.0 * atr, 2)
        target_price = round(sma20, 2)
        if target_price > buy_price and buy_price > stop_price:
            rr = (target_price - buy_price) / (buy_price - stop_price)
            recs.append({
                "sym": sym, "action": "🟢 买入",
                "entry": buy_price, "stop": stop_price, "target": target_price,
                "rr": f"1:{rr:.1f}", "reason": "闪崩后抄底",
            })

    # 买入: 急跌均值回归
    if pct < -5 and not has_bounce:
        buy_price = round(max(dl * 1.01, price * 1.005), 2)
        stop_price = round(buy_price - 2.0 * atr, 2)
        target_price = round(max(vwap, sma20), 2)
        if target_price > buy_price and buy_price > stop_price:
            rr = (target_price - buy_price) / (buy_price - stop_price)
            recs.append({
                "sym": sym, "action": "🟢 买入",
                "entry": buy_price, "stop": stop_price, "target": target_price,
                "rr": f"1:{rr:.1f}", "reason": f"急跌{pct:.0f}%回归",
            })

    # 买入: 强反弹跟进
    if has_bounce:
        for e in recent_events:
            if e["sym"] == sym and e["type"] == "反弹":
                bp_pct = float(e["detail"].split("+")[1].split("%")[0])
                if bp_pct > 4.5:
                    buy_price = round(price, 2)
                    stop_price = round(dl * 0.98, 2)
                    target_price = round(max(vwap, sma20), 2)
                    if target_price > buy_price and buy_price > stop_price:
                        rr = (target_price - buy_price) / (buy_price - stop_price)
                        recs.append({
                            "sym": sym, "action": "🟢 买入",
                            "entry": buy_price, "stop": stop_price, "target": target_price,
                            "rr": f"1:{rr:.1f}", "reason": f"强反弹+{bp_pct:.0f}%跟进",
                        })
                    break

    # 卖出: 急涨减仓
    if has_surge or pct > 5:
        recs.append({
            "sym": sym, "action": "🔴 卖出",
            "entry": round(price, 2), "stop": round(price + 1.0 * atr, 2),
            "target": round(vwap, 2), "rr": "止盈", "reason": "急涨减仓",
        })

    return recs


def _build_email_table(stocks_data: list[dict], events: list[dict],
                       recommendations: list[dict], trigger_reason: str) -> str:
    """构建表格化邮件正文"""
    lines = []
    lines.append(f"触发: {trigger_reason}")
    lines.append("")

    # ── 行情表格 ──
    sep = "─" * 74
    lines.append(f"┌────────┬──────────┬────────┬────────┬────────┬────────┬──────────────┐")
    lines.append(f"│  股票   │   现价    │ 涨跌幅  │  最高   │  最低   │  振幅   │    状态       │")
    lines.append(f"├────────┼──────────┼────────┼────────┼────────┼────────┼──────────────┤")
    for s in stocks_data:
        state_str = ", ".join(s["states"]) if s["states"] else "正常"
        entry_tag = " 🔒" if s.get("entry", 0) > 0 else ""
        lines.append(
            f"│ {s['sym']+entry_tag:6s} │ ${s['price']:7.2f} │ "
            f"{s['pct']:+6.1f}% │ ${s['day_high']:6.2f} │ "
            f"${s['day_low']:6.2f} │ {s['amp']:5.1f}% │ "
            f"{state_str:12s} │"
        )
    lines.append(f"└────────┴──────────┴────────┴────────┴────────┴────────┴──────────────┘")
    lines.append("")

    # ── 近期事件 ──
    if events:
        lines.append(f"┌─ 近期事件 {'─'*60}")
        for e in events[-8:]:
            emoji = {"闪崩": "⚡", "暴涨": "⚡", "反弹": "🟢", "放量": "📊"}.get(e["type"], "•")
            lines.append(f"│ {emoji} {e['sym']:5s} {e['type']}  [{e['window']}]  {e['detail']}")
        lines.append(f"└{'─'*72}")
        lines.append("")

    # ── 操作建议 ──
    if recommendations:
        lines.append(f"┌─ 操作建议 {'─'*58}")
        lines.append(f"│ {'股票':6s} │ {'方向':6s} │ {'入场价':>8s} │ {'止损价':>8s} │ {'止盈价':>8s} │ {'盈亏比':>6s} │ {'理由':12s} │")
        lines.append(f"│{'─'*8}┼{'─'*8}┼{'─'*10}┼{'─'*10}┼{'─'*10}┼{'─'*8}┼{'─'*14}│")
        for r in recommendations:
            lines.append(
                f"│ {r['sym']:6s} │ {r['action']:6s} │ "
                f"${r['entry']:>7.2f} │ ${r['stop']:>7.2f} │ "
                f"${r['target']:>7.2f} │ {r['rr']:>6s} │ {r['reason']:12s} │"
            )
        lines.append(f"└{'─'*74}")
    else:
        lines.append("  💡 当前无明确操作建议，观望为主")

    return "\n".join(lines)


def _should_send_email(events: list[dict], current_minute: int) -> tuple[bool, str]:
    """
    判断是否发送邮件 + 触发原因。
    用 (sym, type, window_start) 做精准去重，避免同一事件重复触发。
    """
    global _event_history, _last_email_minute

    reasons = []
    new_event_count = 0

    for e in events:
        # 用窗口起点做精准去重: 同一事件不会因窗口终点微调而重复
        win_start = e.get("window", "00:00-00:00").split("-")[0]
        key = (e["sym"], e["type"], win_start)
        last = _event_history.get(key, -99)
        if current_minute - last < EVENT_COOLDOWN:
            continue

        triggered = False
        if e["type"] in ("闪崩", "暴涨"):
            reasons.append(f"{e['sym']} {e['type']}")
            triggered = True
        elif e["type"] == "放量":
            ratio = float(e["detail"].split("x")[0][2:])
            if ratio > 10:
                reasons.append(f"{e['sym']} 巨量{ratio:.0f}x")
                triggered = True
        elif e["type"] == "反弹":
            pct = float(e["detail"].split("+")[1].split("%")[0])
            if pct > 5:  # 提高阈值: 4.5% → 5%
                reasons.append(f"{e['sym']} 强反弹+{pct:.0f}%")
                triggered = True

        if triggered:
            _event_history[key] = current_minute
            new_event_count += 1

    # 例行: 距上次>ROUTINE_INTERVAL 且 有新事件 (避免空邮件)
    is_routine = ((current_minute - _last_email_minute) >= ROUTINE_INTERVAL
                  and new_event_count > 0)

    if reasons:
        _last_email_minute = current_minute
        return True, ", ".join(reasons)
    elif is_routine:
        _last_email_minute = current_minute
        return True, f"例行更新 ({new_event_count}个新事件)"
    else:
        return False, ""


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
    from src.analysis.portfolio_strategy import get_strategy_file_path
    import yaml

    strategy_file = get_strategy_file_path()
    if not strategy_file.exists():
        return []

    try:
        with open(strategy_file) as f:
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
