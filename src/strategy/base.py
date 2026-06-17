"""
策略基类
所有策略必须继承此基类并实现 on_bar 方法

设计原则:
    - 策略是纯逻辑，不持有 IB 连接（便于回测和实盘共用）
    - on_bar 接收 DataFrame 行，返回 Signal
    - 策略参数通过 config dict 注入
    - 支持持仓状态跟踪
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from src.strategy.signals import Signal, SignalAction, SignalReason, hold

logger = logging.getLogger(__name__)


@dataclass
class StrategyContext:
    """策略运行时上下文"""
    symbol: str
    # 持仓状态
    position: float = 0.0           # 当前持仓数量
    avg_cost: float = 0.0           # 平均成本
    # 资金
    initial_capital: float = 10000.0
    cash: float = 10000.0
    # 统计
    trade_count: int = 0
    bars_processed: int = 0


class Strategy(ABC):
    """
    策略基类

    使用方法:
        class MyStrategy(Strategy):
            def on_bar(self, bar, context):
                if bar['close'] > bar['sma_20']:
                    return buy(...)
                return hold(...)

        strategy = MyStrategy({"fast": 5, "slow": 20})
        signal = strategy.next(df.iloc[-1], context)
    """

    def __init__(self, params: dict = None, name: str = "", backtest_mode: bool = False):
        """
        Args:
            params: 策略参数字典，如 {"fast_period": 5, "slow_period": 20}
            name: 策略名称
            backtest_mode: 回测模式（True 时不自动更新仓位，由引擎管理）
        """
        self.params = params or {}
        self.name = name or self.__class__.__name__
        self.backtest_mode = backtest_mode
        self._contexts: dict[str, StrategyContext] = {}  # 按 symbol 分离上下文
        logger.info("策略 [%s] 已初始化，参数: %s", self.name, self.params)

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    def on_bar(self, bar: pd.Series, context: StrategyContext) -> Signal:
        """
        每根 K 线触发一次

        Args:
            bar: 当前K线数据（含 OHLCV + 技术指标）
            context: 当前 symbol 的运行时上下文

        Returns:
            Signal — BUY/SELL/HOLD
        """
        ...

    # ------------------------------------------------------------------
    # 可选覆盖
    # ------------------------------------------------------------------

    def on_tick(self, tick: dict, context: StrategyContext) -> Signal:
        """每个 tick 触发（实盘模式）"""
        return hold(context.symbol, self.name)

    def on_news_sentiment(self, sentiment: dict, context: StrategyContext) -> Optional[float]:
        """
        新闻情感回调

        Returns:
            风险调整系数 [0, 2]: <1=减仓, 1=不变, >1=加仓
        """
        return 1.0

    def on_order_filled(self, trade: dict, context: StrategyContext):
        """订单成交回调"""
        pass

    # ------------------------------------------------------------------
    # 模板方法
    # ------------------------------------------------------------------

    def next(self, bar: pd.Series, context: Optional[StrategyContext] = None) -> Signal:
        """
        处理下一根 K 线（模板方法）

        Args:
            bar: 当前K线
            context: 上下文（如果未提供，自动创建或复用）

        Returns:
            Signal
        """
        symbol = context.symbol if context else bar.get("symbol", "UNKNOWN")
        if context is None:
            context = self._get_context(symbol)

        context.bars_processed += 1

        try:
            signal = self.on_bar(bar, context)
        except Exception as e:
            logger.error("策略 [%s] on_bar 异常: %s", self.name, e)
            return hold(symbol, self.name)

        # 更新上下文（回测模式由引擎管理，实盘模式自动更新）
        if signal and signal.is_actionable and not self.backtest_mode:
            self._apply_signal(signal, context, bar)

        return signal

    def get_context(self, symbol: str) -> StrategyContext:
        """获取或创建上下文"""
        return self._get_context(symbol)

    def reset(self, symbol: str = None):
        """重置上下文"""
        if symbol:
            self._contexts.pop(symbol, None)
        else:
            self._contexts.clear()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get_context(self, symbol: str) -> StrategyContext:
        if symbol not in self._contexts:
            self._contexts[symbol] = StrategyContext(symbol=symbol)
        return self._contexts[symbol]

    def _apply_signal(self, signal: Signal, context: StrategyContext, bar: pd.Series):
        """更新上下文状态"""
        price = signal.price or bar.get("close", 0)

        if signal.action == SignalAction.BUY:
            cost = signal.quantity * price
            if cost <= context.cash:
                context.cash -= cost
                # 更新持仓均价
                total_value = context.position * context.avg_cost + cost
                context.position += signal.quantity
                context.avg_cost = total_value / context.position if context.position > 0 else 0
                context.trade_count += 1
        elif signal.action == SignalAction.SELL:
            proceeds = signal.quantity * price
            context.cash += proceeds
            context.position -= signal.quantity
            if context.position <= 0:
                context.position = 0
                context.avg_cost = 0
            context.trade_count += 1

    def __repr__(self) -> str:
        return f"Strategy({self.name}, params={self.params})"
