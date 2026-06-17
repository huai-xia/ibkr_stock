"""
布林带均值回归策略

核心逻辑:
    - 价格触及/跌破布林下轨 → 超卖 → 买入（价格大概率回归中轨）
    - 价格触及/突破布林上轨 → 超买 → 卖出
    - 价格回归中轨 → 平仓

参数:
    bb_period: 布林带周期 (默认 20)
    bb_std: 标准差倍数 (默认 2.0)
    rsi_oversold: RSI 超卖阈值 (默认 30)
    rsi_overbought: RSI 超买阈值 (默认 70)
    stop_loss_pct: 止损 (默认 0.03)
"""

import logging
import pandas as pd
import numpy as np

from src.strategy.base import Strategy, StrategyContext
from src.strategy.signals import Signal, SignalReason, hold, buy, sell

logger = logging.getLogger(__name__)


class MeanReversionStrategy(Strategy):
    """布林带均值回归策略"""

    def __init__(self, params: dict = None):
        defaults = {
            "bb_period": 20,
            "bb_std": 2.0,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "stop_loss_pct": 0.03,
        }
        defaults.update(params or {})
        super().__init__(defaults, name="MeanReversion")

    def on_bar(self, bar: pd.Series, context: StrategyContext) -> Signal:
        close = bar["close"]
        bb_lower = bar.get("bb_lower")
        bb_upper = bar.get("bb_upper")
        bb_middle = bar.get("bb_middle")
        rsi_val = bar.get("rsi_14")

        if any(v is None or np.isnan(v) for v in [bb_lower, bb_upper, bb_middle]):
            return hold(context.symbol, self.name)

        price = close

        # ===== 持仓状态检查 =====
        if context.position > 0 and context.avg_cost > 0:
            # 止损
            pnl_pct = (price - context.avg_cost) / context.avg_cost
            if pnl_pct <= -self.params["stop_loss_pct"]:
                return sell(
                    context.symbol, context.position, price,
                    reason=SignalReason.STOP_LOSS,
                    confidence=1.0, strategy=self.name,
                )

            # 价格回归中轨 → 止盈平仓
            if price >= bb_middle:
                return sell(
                    context.symbol, context.position, price,
                    reason=SignalReason.BB_MIDDLE_RETURN,
                    confidence=0.7, strategy=self.name,
                )

        # ===== 信号判断 =====
        rsi_ok = True
        oversold = price <= bb_lower
        overbought = price >= bb_upper

        if rsi_val is not None and not np.isnan(rsi_val):
            if oversold and rsi_val > self.params["rsi_oversold"]:
                rsi_ok = False
            if overbought and rsi_val < self.params["rsi_overbought"]:
                rsi_ok = False

        quantity = self._calc_quantity(price, context)

        # 触及下轨 + RSI 超卖 → 买入
        if oversold and rsi_ok and context.position <= 0:
            confidence = 0.5 + (self.params["bb_std"] - 1) * 0.25
            return buy(
                context.symbol, quantity, price,
                reason=SignalReason.BB_LOWER_TOUCH,
                confidence=min(confidence, 0.8), strategy=self.name,
            )

        # 触及上轨 + RSI 超买 → 卖出
        if overbought and context.position > 0:
            confidence = 0.5 + (self.params["bb_std"] - 1) * 0.25
            return sell(
                context.symbol, context.position, price,
                reason=SignalReason.BB_UPPER_TOUCH,
                confidence=min(confidence, 0.8), strategy=self.name,
            )

        return hold(context.symbol, self.name)

    def _calc_quantity(self, price: float, context: StrategyContext) -> float:
        """计算买入数量"""
        if price <= 0:
            return 0
        qty = int(context.cash * 0.95 / price)
        return max(0, qty)
