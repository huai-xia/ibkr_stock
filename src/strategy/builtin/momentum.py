"""
双均线动量策略

核心逻辑:
    - 快线（短期均线）上穿慢线（长期均线）→ 金叉，买入
    - 快线下穿慢线 → 死叉，卖出
    - 可配置止损/止盈

参数:
    fast_period: 快线周期 (默认 5)
    slow_period: 慢线周期 (默认 20)
    stop_loss_pct: 止损百分比 (默认 0.05 = 5%)
    take_profit_pct: 止盈百分比 (默认 0.10 = 10%)
    use_macd_confirm: 是否用 MACD 确认信号 (默认 True)
"""

import logging
import pandas as pd
import numpy as np

from src.strategy.base import Strategy, StrategyContext
from src.strategy.signals import Signal, SignalAction, SignalReason, hold, buy, sell

logger = logging.getLogger(__name__)


class MomentumStrategy(Strategy):
    """双均线动量策略"""

    def __init__(self, params: dict = None):
        defaults = {
            "fast_period": 5,
            "slow_period": 20,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.10,
            "use_macd_confirm": True,
        }
        defaults.update(params or {})
        super().__init__(defaults, name="Momentum")

    def on_bar(self, bar: pd.Series, context: StrategyContext) -> Signal:
        fast_key = f"sma_{self.params['fast_period']}"
        slow_key = f"sma_{self.params['slow_period']}"
        fast = bar.get(fast_key)
        slow = bar.get(slow_key)

        if fast is None or slow is None or np.isnan(fast) or np.isnan(slow):
            return hold(context.symbol, self.name)

        current_price = bar["close"]

        # ===== 持仓状态：检查止损止盈 =====
        if context.position > 0 and context.avg_cost > 0:
            pnl_pct = (current_price - context.avg_cost) / context.avg_cost

            # 止损
            if pnl_pct <= -self.params["stop_loss_pct"]:
                return sell(
                    context.symbol, context.position, current_price,
                    reason=SignalReason.STOP_LOSS,
                    confidence=1.0, strategy=self.name,
                    pnl_pct=pnl_pct,
                )

            # 止盈
            if pnl_pct >= self.params["take_profit_pct"]:
                return sell(
                    context.symbol, context.position, current_price,
                    reason=SignalReason.TAKE_PROFIT,
                    confidence=1.0, strategy=self.name,
                    pnl_pct=pnl_pct,
                )

        # ===== 信号判断 =====
        # 需要前一根 K 线的均线值来判断交叉
        # 在回测引擎中，bar 是当前值，我们用 bar 内部的 prev 数据
        prev_fast = bar.get(f"prev_{fast_key}", fast)
        prev_slow = bar.get(f"prev_{slow_key}", slow)

        # 金叉：快线上穿慢线
        golden_cross = prev_fast <= prev_slow and fast > slow
        # 死叉：快线下穿慢线
        dead_cross = prev_fast >= prev_slow and fast < slow

        # MACD 确认
        macd_confirmed = True
        if self.params["use_macd_confirm"]:
            macd_hist = bar.get("macd_hist")
            if macd_hist is not None and not np.isnan(macd_hist):
                # 金叉时 MACD 柱应为正，死叉时应为负
                if golden_cross and macd_hist <= 0:
                    macd_confirmed = False
                if dead_cross and macd_hist >= 0:
                    macd_confirmed = False

        # 生成信号
        quantity = self._calc_quantity(current_price, bar, context)

        if golden_cross and macd_confirmed and context.position <= 0:
            confidence = min(0.8, 0.5 + abs(fast - slow) / slow * 10)
            return buy(
                context.symbol, quantity, current_price,
                reason=SignalReason.SMA_GOLDEN_CROSS,
                confidence=confidence, strategy=self.name,
                fast=round(fast, 2), slow=round(slow, 2),
            )

        if dead_cross and context.position > 0:
            confidence = min(0.8, 0.5 + abs(slow - fast) / slow * 10)
            return sell(
                context.symbol, context.position, current_price,
                reason=SignalReason.SMA_DEAD_CROSS,
                confidence=confidence, strategy=self.name,
                fast=round(fast, 2), slow=round(slow, 2),
            )

        return hold(context.symbol, self.name)

    def _calc_quantity(
        self, price: float, bar: pd.Series, context: StrategyContext,
    ) -> float:
        """计算买入数量（全仓买入，按整数股）"""
        if price <= 0:
            return 0
        # 用 95% 可用资金买入（预留缓冲）
        qty = int(context.cash * 0.95 / price)
        return max(0, qty)
