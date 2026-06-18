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
from src.notify.email import EmailNotifier
from src.trade.portfolio import Portfolio
from src.analysis.signals import SignalDetector
from src.strategy.indicators import add_all
from src.config import get_env, FINNHUB_API_KEY
import yaml

# ── 状态文件 ──
STATUS_FILE = Path("data/monitor_status.txt")
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

    # 读取自选股列表
    watchlist = _load_watchlist()
    log(f"🚀 持仓监控启动 (间隔 {args.interval}秒, 端口 {args.port})")
    log(f"   持仓: (从IBKR获取) | 自选: {len(watchlist)}只")
    log(f"   Ctrl+C 停止")

    iteration = 0

    while running:
        iteration += 1
        log(f"🔍 第 {iteration} 次检查")

        ib = None
        try:
            # 连接
            cm = ConnectionManager(host="127.0.0.1", port=args.port, client_id=1, max_retries=3)
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
                # ── 汇总所有告警（含持仓+自选），统一发一封邮件 ──
                if not args.no_push:
                    email_parts = []

                    # ① 持仓告警
                    if alerts:
                        email_parts.append("## 🛡️ 持仓告警\n")
                        for a in alerts:
                            email_parts.append(f"- {a.message}")
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

                    if deduped_signals or wl_alerts_deduped:
                        email_parts.append("## 📡 自选股异动\n")
                        for s in deduped_signals:
                            email_parts.append(
                                f"- {s['symbol']}: {s['signal']['reason']} | "
                                f"${s['current']:.2f} | 距昨收 {s['day_change_pct']:+.1f}% | "
                                f"{s['fund_flow']}"
                            )
                        for a in wl_alerts_deduped:
                            email_parts.append(f"- {a}")
                        email_parts.append("")

                    if email_parts:
                        # ── 冷却检查：同一告警 30 分钟内不重复发送 ──
                        if _should_send_alert(email_parts):
                            email_body = f"## 📊 IBKR 监控报告\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')} (美东)\n\n"
                            email_body += "\n".join(email_parts)
                            email_body += "\n---\n*🤖 监控守护进程自动推送*"

                            has_critical = any(
                                "跌破" in p or "🚫" in p for p in email_parts
                            )
                            subject = "🚨 IBKR 监控告警" if has_critical else "📊 IBKR 监控报告"
                            _send_email(subject, email_body)
                            log(f"  📧 监控报告已推送")
                        else:
                            log(f"  ⏳ 告警冷却中，跳过推送")

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


# ── 告警冷却：防止同一事件反复发送邮件 ──
_alert_cooldown: dict = {}  # key: "symbol|alert_type" → value: last_send_timestamp
COOLDOWN_SECONDS = 1800  # 30 分钟冷却


def _should_send_alert(email_parts: list[str]) -> bool:
    """检查是否应该发送告警（冷却期外）"""
    global _alert_cooldown
    now = datetime.now().timestamp()

    # 提取告警中的股票+类型+级别
    for line in email_parts:
        sym = ""
        alert_type = ""
        severity = "warning"  # 默认

        # 提取股票代码（第一个大写短词）
        for word in line.replace(":", "").replace("⚠️","").replace("🚫","").replace("🎯","").replace("📌","").replace("🟢","").replace("🟡","").replace("🔴","").replace("🔔","").split():
            w = word.strip("$").strip(".")
            if w.isupper() and 2 <= len(w) <= 5 and w not in ("STRONG", "MEDIUM", "WEAK", "HIGH", "LOW"):
                sym = w
                break

        # 识别告警类型和严重程度
        if "跌破" in line or "🚫" in line or "触发止损" in line:
            alert_type = "stop_loss"
            severity = "critical"
        elif "接近止损" in line or "距离止损" in line:
            alert_type = "stop_loss"
            severity = "warning"
        elif "达到" in line or "止盈目标" in line:
            alert_type = "take_profit"
            severity = "critical"
        elif "接近止盈" in line or "距离止盈" in line or "距目标" in line:
            alert_type = "take_profit"
            severity = "warning"
        elif "加仓价" in line or "触及加仓" in line:
            alert_type = "add_position"
            severity = "critical"
        elif "接近加仓" in line:
            alert_type = "add_position"
            severity = "warning"
        elif "减仓价" in line or "触及减仓" in line:
            alert_type = "reduce_position"
            severity = "critical"
        elif "暴跌" in line or "急跌" in line:
            alert_type = "sharp_drop"
            severity = "critical"
        elif "急涨" in line or "暴涨" in line:
            alert_type = "sharp_rise"
            severity = "critical"
        elif "跳空" in line:
            alert_type = "gap"
            severity = "warning"
        else:
            continue

        if not sym or not alert_type:
            continue

        # 关键：严重程度变了 → 立即发送
        key = f"{sym}|{alert_type}"
        last_sent, last_severity = _alert_cooldown.get(key, (0, ""))

        if severity == "critical" and last_severity == "warning":
            # 升级告警：冷却作废，立即发送
            pass
        elif now - last_sent < COOLDOWN_SECONDS:
            return False  # 冷却中

        _alert_cooldown[key] = (now, severity)

    return True


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
        return notifier.send(subject, body.replace("\n", "<br>"), html=True)
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
