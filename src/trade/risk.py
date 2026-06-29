"""
风控模块
PDT 日内交易限制 + 仓位管理 + 熔断机制

这是整个交易系统的安全核心。所有下单请求必须先通过此模块检查。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from src.trade.recorder import TradeRecorder

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 类型定义
# ------------------------------------------------------------------

class RiskLevel(str, Enum):
    """风控等级"""
    GREEN = "GREEN"       # 正常
    YELLOW = "YELLOW"     # 预警（PDT 接近上限）
    RED = "RED"           # 禁止交易


@dataclass
class RiskResult:
    """风控检查结果"""
    allowed: bool
    reason: str = ""
    level: RiskLevel = RiskLevel.GREEN
    pdt_count: int = 0
    pdt_max: int = 3
    warnings: list[str] = field(default_factory=list)

    def add_warning(self, msg: str):
        self.warnings.append(msg)


# ------------------------------------------------------------------
# 风控管理器
# ------------------------------------------------------------------

class RiskManager:
    """
    多层风控管理器

    检查顺序（短路求值）:
        1. PDT 日内交易限制（硬阻止）
        2. 单票仓位上限
        3. 总持仓上限
        4. 熔断检查（日亏损、连续亏损、周亏损）

    使用方法:
        rm = RiskManager(recorder, account="DU123456", net_liq=10000.00)
        result = rm.check_before_order("AAPL", OrderAction.BUY, 100, 180.0)
        if result.allowed:
            # 下单
        else:
            print(result.reason)
    """

    def __init__(
        self,
        recorder: TradeRecorder,
        account: str = "",
        net_liq: float = 0.0,
        # PDT 配置
        pdt_max: int = 3,
        pdt_window_days: int = 5,
        pdt_warning_threshold: int = 2,
        # 仓位限制
        max_single_stock_pct: float = 0.20,
        max_total_position_pct: float = 0.80,
        # 熔断
        daily_loss_pct: float = 0.05,
        consecutive_losses: int = 3,
        weekly_loss_pct: float = 0.10,
    ):
        self._recorder = recorder
        self.account = account
        self.net_liq = net_liq

        # PDT
        self.pdt_max = pdt_max
        self.pdt_window_days = pdt_window_days
        self.pdt_warning_threshold = pdt_warning_threshold

        # 仓位
        self.max_single_stock_pct = max_single_stock_pct
        self.max_total_position_pct = max_total_position_pct

        # 熔断
        self.daily_loss_pct = daily_loss_pct
        self.consecutive_losses = consecutive_losses
        self.weekly_loss_pct = weekly_loss_pct

        # 运行时状态
        self._daily_pnl = 0.0
        self._consecutive_loss_count = 0
        self._weekly_pnl = 0.0
        self._blocked_until: Optional[datetime] = None

    # ------------------------------------------------------------------
    # 主检查入口
    # ------------------------------------------------------------------

    def check_before_order(
        self,
        symbol: str,
        action,
        quantity: float,
        price: Optional[float] = None,
    ) -> RiskResult:
        """
        下单前全面风控检查

        Args:
            symbol: 股票代码
            action: OrderAction.BUY 或 OrderAction.SELL
            quantity: 数量
            price: 价格（限价单）

        Returns:
            RiskResult (allowed=True 才能下单)
        """
        from src.trade.orders import OrderAction

        result = RiskResult(allowed=True, pdt_max=self.pdt_max)

        # 0. 检查是否在冷却期
        if self._blocked_until and datetime.now() < self._blocked_until:
            remaining = (self._blocked_until - datetime.now()).seconds // 60
            result.allowed = False
            result.level = RiskLevel.RED
            result.reason = f"熔断冷却中，还需等待 {remaining} 分钟"
            return result

        # 1. PDT 检查
        result = self._check_pdt(result, action, symbol)

        # 2. 单票仓位检查（仅买入时）
        if action == OrderAction.BUY:
            result = self._check_single_position(result, symbol, quantity, price)

        # 3. 总仓位检查
        result = self._check_total_position(result, quantity, price)

        # 4. 熔断检查
        result = self._check_circuit_breaker(result)

        # 汇总
        if result.level == RiskLevel.RED:
            result.allowed = False

        return result

    # ------------------------------------------------------------------
    # PDT 日内交易检查
    # ------------------------------------------------------------------

    def _check_pdt(
        self,
        result: RiskResult,
        action,
        symbol: str,
    ) -> RiskResult:
        """检查 PDT 日内交易限制"""
        # PDT 规则仅适用于净清算值 < $25,000 的账户
        PDT_THRESHOLD = 25000.0
        if self.net_liq >= PDT_THRESHOLD:
            result.pdt_count = 0
            return result

        from src.trade.orders import OrderAction

        count = self._recorder.count_day_trades(
            account=self.account,
            window_days=self.pdt_window_days,
        )
        result.pdt_count = count

        # 仅卖出时可能触发日内交易
        # （简化逻辑：同一天有对应买入的卖出算日内交易）
        if action == OrderAction.SELL:
            # 检查当天是否买入过同一股票
            today_trades = self._recorder.query(
                symbol=symbol,
                account=self.account,
                days=1,
                status="FILLED",
            )
            bought_today = any(t["action"] == "BUY" for t in today_trades)

            if bought_today:
                # 这笔卖出构成日内交易
                count += 1  # 即将增加的次数

        # 评估风险等级
        if count >= self.pdt_max:
            result.level = RiskLevel.RED
            result.reason = (
                f"🚫 PDT 限制: 5日内已有 {count} 次日内交易（上限 {self.pdt_max}），"
                f"本次卖出将导致违规！"
            )
            result.add_warning(f"PDT 超限: {count}/{self.pdt_max}")
        elif count >= self.pdt_warning_threshold:
            result.level = RiskLevel.YELLOW
            result.add_warning(
                f"⚠️ PDT 预警: 已进行 {count}/{self.pdt_max} 次日内交易，"
                f"仅剩 {self.pdt_max - count} 次额度"
            )
        else:
            # 有日内交易但未达预警线
            if count > 0:
                result.add_warning(f"ℹ️ PDT 计数: {count}/{self.pdt_max}")

        return result

    # ------------------------------------------------------------------
    # 仓位检查
    # ------------------------------------------------------------------

    def _check_single_position(
        self,
        result: RiskResult,
        symbol: str,
        quantity: float,
        price: Optional[float],
    ) -> RiskResult:
        """检查单票仓位上限"""
        if self.net_liq <= 0 or price is None or price <= 0:
            return result

        order_value = quantity * price
        position_pct = order_value / self.net_liq

        if position_pct > self.max_single_stock_pct:
            result.level = max(result.level, RiskLevel.YELLOW)
            result.add_warning(
                f"单票仓位 {position_pct:.1%} 超过上限 {self.max_single_stock_pct:.1%}"
            )

        return result

    def _check_total_position(
        self,
        result: RiskResult,
        quantity: float,
        price: Optional[float],
    ) -> RiskResult:
        """检查总持仓上限"""
        # 简化：检查是否还有现金可用
        if self.net_liq <= 0:
            return result

        # 这里只是占位逻辑，实际需要从 IB 获取当前总持仓
        available_pct = 1.0 - self.max_total_position_pct
        if available_pct <= 0:
            result.level = RiskLevel.RED
            result.reason = "总持仓已达上限，无可投资金"
        elif price and quantity * price > self.net_liq * 0.15:
            result.add_warning(
                f"单笔交易金额 ${quantity * price:,.2f}，占净清算值 "
                f"{quantity * price / self.net_liq:.1%}"
            )

        return result

    # ------------------------------------------------------------------
    # 熔断检查
    # ------------------------------------------------------------------

    def _check_circuit_breaker(self, result: RiskResult) -> RiskResult:
        """检查熔断条件"""

        # 日内亏损熔断
        if self._daily_pnl < 0 and self.net_liq > 0:
            daily_loss_ratio = abs(self._daily_pnl) / self.net_liq
            if daily_loss_ratio > self.daily_loss_pct:
                result.level = RiskLevel.RED
                result.reason = f"日亏损熔断: 今日亏损 {daily_loss_ratio:.1%} > 上限 {self.daily_loss_pct:.1%}"
                return result

        # 连续亏损熔断
        if self._consecutive_loss_count >= self.consecutive_losses:
            self._blocked_until = datetime.now() + timedelta(minutes=30)
            result.level = RiskLevel.RED
            result.reason = f"连续亏损熔断: 连续 {self._consecutive_loss_count} 笔亏损，暂停交易 30 分钟"
            return result

        # 周亏损熔断
        if self._weekly_pnl < 0 and self.net_liq > 0:
            weekly_loss_ratio = abs(self._weekly_pnl) / self.net_liq
            if weekly_loss_ratio > self.weekly_loss_pct:
                result.level = RiskLevel.RED
                result.reason = f"周亏损熔断: 本周亏损 {weekly_loss_ratio:.1%} > 上限 {self.weekly_loss_pct:.1%}"
                return result

        return result

    # ------------------------------------------------------------------
    # 交易后更新
    # ------------------------------------------------------------------

    def on_trade_filled(self, pnl: float):
        """
        成交后更新状态（由订单模块或策略调用）

        Args:
            pnl: 该笔交易的盈亏
        """
        self._daily_pnl += pnl
        self._weekly_pnl += pnl

        if pnl < 0:
            self._consecutive_loss_count += 1
            logger.warning(
                "连续亏损: %d/%d (本笔 PNL: $%.2f)",
                self._consecutive_loss_count, self.consecutive_losses, pnl,
            )
        else:
            self._consecutive_loss_count = 0  # 盈利时重置计数

    def on_new_day(self):
        """新交易日重置日级状态"""
        self._daily_pnl = 0.0
        self._blocked_until = None
        logger.info("新交易日开始，日级风控状态已重置")

    def on_new_week(self):
        """新周重置周级状态"""
        self._weekly_pnl = 0.0
        logger.info("新交易周开始，周级风控状态已重置")

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_pdt_status(self) -> dict:
        """获取 PDT 状态（净资产 ≥ $25,000 时 PDT 不适用）"""
        PDT_THRESHOLD = 25000.0
        if self.net_liq >= PDT_THRESHOLD:
            return {
                "count": 0,
                "max": self.pdt_max,
                "remaining": self.pdt_max,
                "level": RiskLevel.GREEN.value,
                "window_days": self.pdt_window_days,
                "pdt_applies": False,
                "net_liq": self.net_liq,
                "details": [],
            }

        count = self._recorder.count_day_trades(
            account=self.account,
            window_days=self.pdt_window_days,
        )
        remaining = max(0, self.pdt_max - count)

        if count >= self.pdt_max:
            level = RiskLevel.RED
        elif count >= self.pdt_warning_threshold:
            level = RiskLevel.YELLOW
        else:
            level = RiskLevel.GREEN

        return {
            "count": count,
            "max": self.pdt_max,
            "remaining": remaining,
            "level": level.value,
            "window_days": self.pdt_window_days,
            "pdt_applies": True,
            "net_liq": self.net_liq,
            "details": self._recorder.get_day_trade_details(
                account=self.account,
                window_days=self.pdt_window_days,
            ),
        }

    def get_risk_status(self) -> dict:
        """获取完整风控状态"""
        return {
            "pdt": self.get_pdt_status(),
            "daily_pnl": self._daily_pnl,
            "weekly_pnl": self._weekly_pnl,
            "consecutive_losses": self._consecutive_loss_count,
            "blocked_until": str(self._blocked_until) if self._blocked_until else None,
        }
