"""
持仓监控器
实时扫描持仓，触达止损/止盈线自动告警

使用方式:
    # 一次性检查
    python3 -m src.cli.main --port 4002 monitor

    # 循环监控（每5分钟检查一次）
    python3 -m src.cli.main --port 4002 monitor --loop --interval 300
"""

import logging
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from src.core.connection import ConnectionManager
from src.analysis.stock_data import StockDataManager
from src.analysis.exit_strategy import ExitStrategyEngine, ExitPlan
from src.data.price_validator import PriceValidator
from src.notify.email import EmailNotifier
from src.trade.portfolio import Portfolio
from src.config import get_env, FINNHUB_API_KEY

logger = logging.getLogger(__name__)


@dataclass
class PositionAlert:
    """持仓告警"""
    symbol: str
    alert_type: str          # stop_loss / take_profit / near_stop / near_target
    level: str               # critical / warning / info
    current_price: float
    threshold_price: float
    distance_pct: float
    message: str
    timestamp: str = ""


class PositionMonitor:
    """
    持仓监控器

    流程:
        持仓数据 → 退出策略 → 实时价格 → 距离计算 → 告警判断 → 推送
    """

    def __init__(self, ib):
        self._ib = ib
        self._portfolio = Portfolio(ib)
        self._validator = PriceValidator(ib, finnhub_key=FINNHUB_API_KEY)
        self._engine = ExitStrategyEngine()
        self._data_mgr = StockDataManager(ib)

    # ------------------------------------------------------------------
    # 单次检查
    # ------------------------------------------------------------------

    def check(self, risk_profile: str = "moderate") -> list[PositionAlert]:
        """
        扫描所有持仓，检查是否需要告警

        Returns:
           告警列表
        """
        alerts = []
        positions = self._portfolio.get_positions()
        active = [p for p in positions if p.get("position", 0) != 0]

        if not active:
            logger.info("当前无持仓")
            return alerts

        for pos in active:
            symbol = pos["symbol"]
            entry_price = pos["avg_cost"]
            shares = abs(pos["position"])

            if entry_price <= 0:
                continue

            # 1. 获取实时价格
            price_result = self._validator.get_price(symbol)
            current_price = price_result.price
            if current_price <= 0:
                continue

            # 2. 加载历史数据 + 计算退出策略
            try:
                df = self._data_mgr.load(symbol)
                if df is None or df.empty:
                    logger.debug("%s 无缓存数据，跳过", symbol)
                    continue
            except:
                continue

            plan = self._engine.analyze(
                symbol, df, entry_price=entry_price,
                current_price=current_price, risk_profile=risk_profile,
            )
            if plan is None:
                continue

            # 3. 距离计算
            alerts.extend(self._check_thresholds(symbol, current_price, plan, shares))

        return alerts

    def _check_thresholds(
        self, symbol: str, price: float, plan: ExitPlan, shares: float,
    ) -> list[PositionAlert]:
        """检查各阈值"""
        alerts = []
        ts = datetime.now().strftime("%H:%M:%S")

        # 止损距离
        stop_dist = (price - plan.normal_stop) / price * 100 if price > 0 else 0
        target_dist = (plan.target_2 - price) / price * 100 if price > 0 else 0

        # 🔴 止损触发
        if price <= plan.normal_stop:
            alerts.append(PositionAlert(
                symbol=symbol, alert_type="stop_loss", level="critical",
                current_price=price, threshold_price=plan.normal_stop,
                distance_pct=round(stop_dist, 1),
                message=f"🚫 {symbol} 触发止损！${price:.2f} ≤ ${plan.normal_stop:.2f}，建议立即平仓",
                timestamp=ts,
            ))

        # 🟡 接近止损（距离 < 2%）
        elif 0 < stop_dist < 2:
            alerts.append(PositionAlert(
                symbol=symbol, alert_type="near_stop", level="warning",
                current_price=price, threshold_price=plan.normal_stop,
                distance_pct=round(stop_dist, 1),
                message=f"⚠️ {symbol} 接近止损线！${price:.2f}，距离止损 ${plan.normal_stop:.2f} 仅 {stop_dist:.1f}%",
                timestamp=ts,
            ))

        # 🟢 接近止盈（距离 < 5%）
        if 0 < target_dist < 5:
            alerts.append(PositionAlert(
                symbol=symbol, alert_type="near_target", level="info",
                current_price=price, threshold_price=plan.target_2,
                distance_pct=round(target_dist, 1),
                message=f"🎯 {symbol} 接近止盈目标！${price:.2f}，距目标 ${plan.target_2:.2f} 仅 {target_dist:.1f}%",
                timestamp=ts,
            ))

        # 🟢 止盈触发
        if price >= plan.target_2:
            alerts.append(PositionAlert(
                symbol=symbol, alert_type="take_profit", level="info",
                current_price=price, threshold_price=plan.target_2,
                distance_pct=round(target_dist, 1),
                message=f"✅ {symbol} 达成止盈目标！${price:.2f} ≥ ${plan.target_2:.2f}，建议考虑减仓",
                timestamp=ts,
            ))

        return alerts

    # ------------------------------------------------------------------
    # 获取全貌（不告警，只展示）
    # ------------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """获取所有持仓的当前状态快照"""
        positions = self._portfolio.get_positions()
        active = [p for p in positions if p.get("position", 0) != 0]
        snapshots = []

        for pos in active:
            symbol = pos["symbol"]
            shares = abs(pos["position"])
            entry = pos["avg_cost"]

            price_result = self._validator.get_price(symbol)
            current = price_result.price

            pnl = (current - entry) * shares if current > 0 and entry > 0 else 0
            pnl_pct = (current - entry) / entry * 100 if entry > 0 else 0

            snap = {
                "symbol": symbol,
                "shares": shares,
                "entry": entry,
                "current": current,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 1),
                "confidence": price_result.confidence,
            }

            # 尝试计算止损止盈
            try:
                df = self._data_mgr.load(symbol)
                if df is not None:
                    plan = self._engine.analyze(symbol, df, entry_price=entry, current_price=current)
                    if plan:
                        snap["stop"] = plan.normal_stop
                        snap["target"] = plan.target_2
                        snap["stop_dist_pct"] = round((current - plan.normal_stop) / current * 100, 1) if current > 0 else 0
                        snap["target_dist_pct"] = round((plan.target_2 - current) / current * 100, 1) if current > 0 else 0
                        snap["risk_reward"] = plan.risk_reward_ratio
            except:
                pass

            snapshots.append(snap)

        return snapshots

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------

    def format_snapshot(self, snapshots: list[dict]) -> str:
        """格式化持仓快照"""
        if not snapshots:
            return "当前无持仓"

        total_pnl = sum(s.get("pnl", 0) for s in snapshots)

        lines = [
            "## 📊 持仓监控",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            f"| 股票 | 持仓 | 成本 | 现价 | 盈亏 | 止损 | 止盈 |",
            f"|------|------|------|------|------|------|------|",
        ]

        for s in snapshots:
            pnl_str = f"${s['pnl']:+.0f}" if s.get("pnl") else "—"
            stop_str = f"${s['stop']:.2f}" if s.get("stop") else "—"
            target_str = f"${s['target']:.2f}" if s.get("target") else "—"
            lines.append(
                f"| {s['symbol']} | {s['shares']:.0f}股 | ${s['entry']:.2f} | "
                f"${s['current']:.2f} | {pnl_str} | {stop_str} | {target_str} |"
            )

        lines.append("")
        lines.append(f"💰 总浮动盈亏: **${total_pnl:+,.2f}**")

        return "\n".join(lines)

    def format_alerts(self, alerts: list[PositionAlert]) -> str:
        """格式化告警"""
        if not alerts:
            return "✅ 所有持仓正常，无告警"

        lines = ["## ⚠️ 持仓告警", ""]
        for a in alerts:
            emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}
            lines.append(f"{emoji.get(a.level, '')} [{a.timestamp}] {a.message}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def run_loop(self, interval: int = 300, max_iterations: int = 0, push: bool = True):
        """
        循环监控模式

        Args:
            interval: 检查间隔（秒），默认 300（5分钟）
            max_iterations: 最大循环次数（0=无限）
            push: 是否推送告警
        """
        iteration = 0
        logger.info("🔄 持仓监控已启动，间隔 %d 秒", interval)

        try:
            while True:
                iteration += 1
                ts = datetime.now().strftime("%H:%M:%S")
                logger.info("检查 #%d [%s]", iteration, ts)

                # 检查
                alerts = self.check()

                # 展示
                snapshots = self.snapshot()
                print(f"\n{self.format_snapshot(snapshots)}")

                if alerts:
                    print(f"\n{self.format_alerts(alerts)}")
                    if push:
                        self.push_alerts(alerts)
                else:
                    print("✅ 无告警")

                # 退出条件
                if max_iterations > 0 and iteration >= max_iterations:
                    break

                print(f"⏰ 下次检查: {interval}秒后...")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👋 监控已停止")

    def push_alerts(self, alerts: list[PositionAlert]) -> bool:
        """推送告警到邮箱（多条股票预警合并为一封邮件）"""
        if not alerts:
            return True

        smtp_user = get_env("SMTP_USER", "")
        smtp_password = get_env("SMTP_PASSWORD", "")
        if not smtp_user or not smtp_password:
            return False

        notifier = EmailNotifier(
            smtp_host=get_env("SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(get_env("SMTP_PORT", "587")),
            user=smtp_user,
            password=smtp_password,
        )

        # === 构建合并邮件 ===
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        symbols = sorted(set(a.symbol for a in alerts))
        n_critical = sum(1 for a in alerts if a.level == "critical")
        n_warning = sum(1 for a in alerts if a.level == "warning")
        n_info = sum(1 for a in alerts if a.level == "info")

        # 标题：含股票数和关键级别
        if n_critical > 0:
            subject = f"🚨 IBKR 告警 — {len(symbols)}只股票 · {n_critical}条紧急"
        elif n_warning > 0:
            subject = f"⚠️ IBKR 预警 — {len(symbols)}只股票 · {n_warning}条警告"
        else:
            subject = f"📊 IBKR 提醒 — {len(symbols)}只股票接近目标"

        # HTML 邮件正文
        level_labels = []
        if n_critical:
            level_labels.append(f"🔴 紧急 {n_critical}")
        if n_warning:
            level_labels.append(f"🟡 警告 {n_warning}")
        if n_info:
            level_labels.append(f"🟢 提示 {n_info}")

        # 按股票分组
        by_symbol: dict[str, list[PositionAlert]] = {}
        for a in alerts:
            by_symbol.setdefault(a.symbol, []).append(a)

        rows_html = ""
        for sym in sorted(by_symbol.keys()):
            stock_alerts = by_symbol[sym]
            # 取该股票最严重的级别决定行底色
            max_level = "critical" if any(a.level == "critical" for a in stock_alerts) else \
                        "warning" if any(a.level == "warning" for a in stock_alerts) else "info"
            bg = {"critical": "#fff0f0", "warning": "#fffbe6", "info": "#f0fff0"}[max_level]
            emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}[max_level]

            for i, a in enumerate(stock_alerts):
                level_tag = {"critical": "紧急", "warning": "警告", "info": "提示"}[a.level]
                rows_html += f"""
                <tr style="background:{bg}">
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:bold">
                        {emoji if i == 0 else '&nbsp;&nbsp;&nbsp;'} {sym if i == 0 else ''}
                    </td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee">
                        <span style="color:{'#d00' if a.level == 'critical' else '#e67e00' if a.level == 'warning' else '#27ae60'}">
                            [{level_tag}]
                        </span>
                    </td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee">
                        现价 <b>${a.current_price:.2f}</b>
                    </td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee">
                        阈值 <b>${a.threshold_price:.2f}</b>
                    </td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee">
                        距阈值 <b>{a.distance_pct:+.1f}%</b>
                    </td>
                </tr>"""

        body = f"""<!DOCTYPE html>
        <html><head><meta charset="utf-8"></head><body>
        <div style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden">

        <!-- 头部 -->
        <div style="background:#1a1a2e;color:white;padding:16px 20px">
            <h2 style="margin:0;font-size:18px">{'🚨' if n_critical else '⚠️'} IBKR 持仓监控告警</h2>
            <p style="margin:6px 0 0;opacity:0.7;font-size:12px">⏰ {now} &nbsp;|&nbsp; 📮 本次检查合并 {len(alerts)} 条预警 → 1 封邮件</p>
        </div>

        <!-- 摘要 -->
        <div style="padding:16px 20px;background:#fafafa;border-bottom:1px solid #e0e0e0">
            <table style="width:100%;text-align:center">
                <tr>
                    <td style="font-size:24px;font-weight:bold">{len(symbols)}</td>
                    <td style="font-size:24px;font-weight:bold;color:#d00">{n_critical}</td>
                    <td style="font-size:24px;font-weight:bold;color:#e67e00">{n_warning}</td>
                    <td style="font-size:24px;font-weight:bold;color:#27ae60">{n_info}</td>
                </tr>
                <tr style="font-size:11px;color:#888">
                    <td>涉及股票</td><td>紧急</td><td>警告</td><td>提示</td>
                </tr>
            </table>
            <p style="margin:8px 0 0;font-size:12px;color:#666">
                涉及股票：{'、'.join(symbols)}
            </p>
        </div>

        <!-- 详表 -->
        <div style="padding:8px 0">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="background:#f5f5f5;color:#555;font-size:11px;text-align:left">
                    <th style="padding:8px 12px">股票</th>
                    <th style="padding:8px 12px">级别</th>
                    <th style="padding:8px 12px">现价</th>
                    <th style="padding:8px 12px">阈值</th>
                    <th style="padding:8px 12px">距离</th>
                </tr>{rows_html}
            </table>
        </div>

        <!-- 底部 -->
        <div style="padding:12px 20px;background:#fafafa;border-top:1px solid #e0e0e0;font-size:11px;color:#999">
            🤖 由 IBKR 交易助手自动发送 &nbsp;|&nbsp; 下次检查 {300}秒后 &nbsp;|&nbsp;
            <a href="https://github.com/huai-xia/ibkr_stock" style="color:#666">ibkr_stock</a>
        </div>

        </div></body></html>"""

        return notifier.send(subject, body, html=True)
