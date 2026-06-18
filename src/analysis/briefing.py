"""
每日简报生成器（收盘版）
盘后综合报告：今日复盘 + 持仓 + 自选信号 + 明日日历 + 操作建议

自动发送:
    python3 scripts/monitor_daemon.py --briefing   ← 手动触发
    launchd 定时任务                                ← 自动 16:15 ET
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from src.analysis.monitor import PositionMonitor
from src.analysis.exit_strategy import ExitStrategyEngine
from src.analysis.stock_data import StockDataManager
from src.analysis.signals import SignalDetector
from src.strategy.indicators import add_all
from src.trade.portfolio import Portfolio
from src.data.price_validator import PriceValidator
from src.news.reporter import MarketReporter
from src.notify.email import EmailNotifier
from src.config import get_env, FINNHUB_API_KEY

logger = logging.getLogger(__name__)


class DailyBriefing:
    """收盘每日简报"""

    def __init__(self, ib):
        self._ib = ib
        self._portfolio = Portfolio(ib)
        self._validator = PriceValidator(ib, finnhub_key=FINNHUB_API_KEY)
        self._engine = ExitStrategyEngine()
        self._data_mgr = StockDataManager(ib)
        self._detector = SignalDetector()
        self._reporter = MarketReporter()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def generate(self) -> str:
        """生成完整收盘简报"""
        now = datetime.now()
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

        lines = [
            "# 📰 IBKR 每日收盘简报",
            f"**{now.strftime('%Y年%m月%d日')} {weekday} {now.strftime('%H:%M')} (美东)**",
            "",
            "---",
            "",
        ]

        # 1. 大盘
        lines.extend(self._section_market())

        # 2. 持仓日终报告
        lines.extend(self._section_holdings())

        # 3. 自选股日线信号
        lines.extend(self._section_watchlist())

        # 4. 今日重要新闻
        lines.extend(self._section_news())

        # 5. 明日财经日历
        lines.extend(self._section_calendar())

        # 6. PDT + 风控提醒
        lines.extend(self._section_risk())

        # 7. 操作建议
        lines.extend(self._section_advice())

        lines.append("---")
        lines.append(f"*🤖 本简报由 IBKR 交易助手自动生成 · {now.strftime('%H:%M:%S')}*")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 各板块
    # ------------------------------------------------------------------

    def _section_market(self) -> list[str]:
        """大盘表现"""
        lines = ["## 📈 大盘表现", ""]

        try:
            for etf, name in [("SPY", "标普500"), ("QQQ", "纳斯达克100"), ("SOXL", "半导体3x")]:
                r = self._validator.get_price(etf)
                if r.price > 0:
                    # 拉昨天收盘对比
                    df = self._data_mgr.load(etf)
                    if df is not None and len(df) >= 2:
                        prev_close = df["close"].iloc[-2]
                        change = (r.price - prev_close) / prev_close * 100
                        emoji = "🟢" if change > 0 else ("🔴" if change < 0 else "⚪")
                        lines.append(f"| {name} ({etf}) | ${r.price:.2f} | {emoji} {change:+.2f}% |")
                    else:
                        lines.append(f"| {name} ({etf}) | ${r.price:.2f} | — |")
            lines.append("")
        except Exception as e:
            lines.append(f"⚠️ 大盘数据获取失败: {e}")
            lines.append("")

        return lines

    def _section_holdings(self) -> list[str]:
        """持仓日终报告"""
        lines = ["## 💼 持仓日终报告", ""]

        try:
            positions = self._portfolio.get_positions()
            active = [p for p in positions if p.get("position", 0) != 0]

            if not active:
                lines.append("> 当前无持仓")
                lines.append("")
                return lines

            lines.append(f"| 股票 | 持仓 | 成本 | 现价 | 浮动盈亏 | 🛑止损 | 🎯止盈 | 加仓 | 减仓 | 信号 |")
            lines.append(f"|------|------|------|------|----------|--------|--------|------|------|------|")

            monitor = PositionMonitor(self._ib)
            snapshots = monitor.snapshot()

            # 加载策略文件中的自定义字段
            strategy_data = {}
            try:
                from src.analysis.portfolio_strategy import STRATEGY_FILE
                import yaml
                if STRATEGY_FILE.exists():
                    with open(STRATEGY_FILE) as f:
                        strategy_data = yaml.safe_load(f) or {}
            except:
                pass
            strat_holdings = strategy_data.get("holdings", {})

            for s in snapshots:
                sym = s["symbol"]
                pnl_str = f"${s.get('pnl', 0):+.0f}" if s.get('pnl') else "—"
                stop_str = f"${s['stop']:.2f}" if s.get('stop') else "—"
                target_str = f"${s['target']:.2f}" if s.get('target') else "—"

                # 策略文件中的自定义值优先
                sh = strat_holdings.get(sym, {})
                add_str = f"${sh['add_on_dip']:.2f}" if sh.get("add_on_dip") else "—"
                reduce_str = f"${sh['reduce_on_rip']:.2f}" if sh.get("reduce_on_rip") else "—"
                if sh.get("stop_loss") and sh["stop_loss"] != s.get("stop"):
                    stop_str = f"${sh['stop_loss']:.2f}*"
                if sh.get("take_profit") and sh["take_profit"] != s.get("target"):
                    target_str = f"${sh['take_profit']:.2f}*"

                # 日线信号
                sig_text = "—"
                try:
                    df = self._data_mgr.load(sym)
                    if df is not None and not df.empty:
                        df = add_all(df)
                        sigs = self._detector.scan(sym, df)
                        if sigs:
                            sig_text = sigs[0].reason[:25]
                except:
                    pass

                lines.append(
                    f"| {sym} | {s['shares']:.0f}股 | ${s['entry']:.2f} | "
                    f"${s['current']:.2f} | {pnl_str} | {stop_str} | {target_str} | "
                    f"{add_str} | {reduce_str} | {sig_text} |"
                )

            total_pnl = sum(s.get("pnl", 0) for s in snapshots)
            lines.append("")
            lines.append(f"💰 总浮动盈亏: **${total_pnl:+,.2f}**")
            lines.append("")

        except Exception as e:
            lines.append(f"⚠️ 持仓分析失败: {e}")
            lines.append("")

        return lines

    def _section_watchlist(self) -> list[str]:
        """自选股日线信号"""
        lines = ["## 📡 自选股日线信号", ""]

        try:
            # 读取自选股
            from scripts.monitor_daemon import _load_watchlist
            watchlist = _load_watchlist()

            # 排除持仓
            positions = self._portfolio.get_positions()
            held = {p["symbol"] for p in positions if p.get("position", 0) != 0}

            signals_all = []
            for item in watchlist:
                sym = item["symbol"]
                if sym in held:
                    continue

                try:
                    df = self._data_mgr.load(sym)
                    if df is None or df.empty:
                        continue
                    df = add_all(df)
                    sigs = self._detector.scan(sym, df)
                    for sig in sigs:
                        signals_all.append(sig)
                except:
                    pass

            if signals_all:
                buy_sigs = [s for s in signals_all if s.signal_type == "buy" and s.strength == "strong"]
                alert_sigs = [s for s in signals_all if s.signal_type == "alert" and s.strength == "strong"]

                if buy_sigs:
                    lines.append("### 🟢 强烈买入信号")
                    for s in buy_sigs[:5]:
                        lines.append(f"- **{s.symbol}**: {s.reason}")
                        if s.note:
                            lines.append(f"  💡 {s.note}")
                    lines.append("")

                if alert_sigs:
                    lines.append("### 🟡 需关注")
                    for s in alert_sigs[:5]:
                        lines.append(f"- **{s.symbol}**: {s.reason}")
                    lines.append("")

                if not buy_sigs and not alert_sigs:
                    lines.append("> 自选股当前无明显日线信号")
                    lines.append("")
            else:
                lines.append("> 自选股数据不足或暂无信号")
                lines.append("")

        except Exception as e:
            lines.append(f"⚠️ 自选股分析失败: {e}")
            lines.append("")

        return lines

    def _section_news(self) -> list[str]:
        """今日重要新闻摘要"""
        lines = ["## 📰 今日要闻", ""]

        try:
            reporter = MarketReporter()
            report = reporter.generate(holdings=[])

            # 提取市场情绪 + 前5条新闻
            for section in report.split("## "):
                if section.startswith("📊 市场总览"):
                    lines.append("## 📊 市场总览")
                    content = section.replace("📊 市场总览", "").strip()
                    # 只取前15行
                    lines.extend(content.split("\n")[:15])
                    lines.append("")
                elif section.startswith("📋 重要新闻列表"):
                    lines.append("### 今日重要新闻")
                    content = section.replace("📋 重要新闻列表", "").strip()
                    lines.extend(content.split("\n")[:15])
                    lines.append("")

        except Exception as e:
            lines.append(f"⚠️ 新闻获取失败: {e}")
            lines.append("")

        return lines

    def _section_calendar(self) -> list[str]:
        """明日财经日历"""
        lines = ["## 📅 明日财经日历", ""]

        try:
            # Finnhub 财报日历
            key = FINNHUB_API_KEY
            if key and "YOUR_KEY" not in key:
                # 财报
                try:
                    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                    resp = requests.get(
                        "https://finnhub.io/api/v1/calendar/earnings",
                        params={"from": tomorrow, "to": tomorrow, "token": key},
                        timeout=10,
                    )
                    data = resp.json()
                    earnings = data.get("earningsCalendar", []) if isinstance(data, dict) else data
                    if earnings:
                        lines.append("### 📊 财报发布")
                        for e in earnings[:8]:
                            sym = e.get("symbol", "")
                            lines.append(f"- **{sym}**: {e.get('hour', '')} | "
                                         f"预估EPS {e.get('epsEstimate', 'N/A')}")
                        lines.append("")
                except:
                    pass

                # 经济数据
                try:
                    resp2 = requests.get(
                        "https://finnhub.io/api/v1/calendar/economic",
                        params={"from": tomorrow, "to": tomorrow, "token": key},
                        timeout=10,
                    )
                    eco = resp2.json()
                    events = eco.get("economicCalendar", []) if isinstance(eco, dict) else eco
                    if events:
                        lines.append("### 🏦 经济数据")
                        for e in events[:6]:
                            lines.append(f"- {e.get('event', '')}: "
                                         f"前值 {e.get('previous', 'N/A')} | "
                                         f"预测 {e.get('estimate', 'N/A')}")
                        lines.append("")
                except:
                    pass

            if not lines[-1].startswith("-"):
                lines.append("> 明日无重要财报或经济数据")
                lines.append("")

        except Exception as e:
            lines.append(f"⚠️ 日历获取失败: {e}")
            lines.append("")

        return lines

    def _section_risk(self) -> list[str]:
        """风控提醒"""
        lines = ["## 🛡️ 风控状态", ""]

        try:
            from src.trade.recorder import TradeRecorder
            from src.trade.risk import RiskManager
            recorder = TradeRecorder("data/trade.db")
            risk = RiskManager(recorder)
            pdt = risk.get_pdt_status()

            pdt_emoji = "🟢" if pdt["count"] < 2 else ("🟡" if pdt["count"] < 3 else "🔴")
            lines.append(f"| 指标 | 状态 |")
            lines.append(f"|------|------|")
            lines.append(f"| PDT 日内交易 | {pdt_emoji} {pdt['count']}/{pdt['max']} 次 |")

            # 账户
            summary = self._portfolio.get_account_summary()
            net_liq = summary.get("net_liquidation", 0)
            lines.append(f"| 账户净值 | ${net_liq:,.2f} |")
            lines.append(f"| 可用资金 | ${summary.get('available_funds', 0):,.2f} |")

            lines.append("")
            lines.append("- 单笔最大亏损 ≤ 总资金 2%（约 ${:.0f}）".format(net_liq * 0.02))
            lines.append("- 单票最大仓位 ≤ 总资金 20%")
            lines.append("")

        except Exception as e:
            lines.append(f"⚠️ 风控数据获取失败: {e}")
            lines.append("")

        return lines

    def _section_advice(self) -> list[str]:
        """次日操作建议"""
        lines = ["## 💡 次日关注", ""]

        # 周末提醒
        now = datetime.now()
        if now.weekday() == 4:  # 周五
            lines.append("- 📌 **周末前**：检查所有持仓止损位，避免周末突发事件风险")
            lines.append("- 📌 PDT 计数将在下周一重置")
            lines.append("")

        lines.append("- 🕐 明日常规交易时段: 9:30-16:00 ET")
        lines.append("- 🛡️ 入场前确认已设止损")
        lines.append("- 📋 操作后运行 `overview --sync` 同步交易记录")
        lines.append("- 📧 需要盘中实时监控请运行 `python3 scripts/monitor_daemon.py`")
        lines.append("")

        return lines

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def push(self, report: str = "", subject: str = "") -> bool:
        if not report:
            return False

        smtp_user = get_env("SMTP_USER", "")
        smtp_password = get_env("SMTP_PASSWORD", "")
        if not smtp_user or not smtp_password:
            return False

        notifier = EmailNotifier(
            smtp_host=get_env("SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(get_env("SMTP_PORT", "587")),
            user=smtp_user, password=smtp_password,
        )

        now = datetime.now()
        subj = subject or f"IBKR 收盘简报 — {now.strftime('%Y-%m-%d')}"

        return notifier.send(subj, report.replace("\n", "<br>"), html=True)
