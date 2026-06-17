"""
交易信号定义
"""

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class SignalReason(str, Enum):
    """信号触发原因"""
    # 动量策略
    SMA_GOLDEN_CROSS = "SMA_金叉"
    SMA_DEAD_CROSS = "SMA_死叉"
    # 均值回归
    BB_LOWER_TOUCH = "布林下轨_超卖"
    BB_UPPER_TOUCH = "布林上轨_超买"
    BB_MIDDLE_RETURN = "布林中轨_回归"
    # 通用
    STOP_LOSS = "止损触发"
    TAKE_PROFIT = "止盈触发"
    NEWS_SENTIMENT = "新闻情感"
    RISK_ADJUST = "风控调整"
    MANUAL = "手动"


@dataclass
class Signal:
    """交易信号"""
    action: SignalAction
    symbol: str
    quantity: float = 0.0
    price: Optional[float] = None
    order_type: str = "LIMIT"           # MARKET / LIMIT / STOP
    reason: SignalReason = SignalReason.MANUAL
    confidence: float = 0.5             # 信号置信度 [0, 1]
    strategy: str = ""                  # 策略名称
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: dict = field(default_factory=dict)  # 额外上下文

    @property
    def is_actionable(self) -> bool:
        """是否需要执行下单"""
        return self.action in (SignalAction.BUY, SignalAction.SELL) and self.quantity > 0

    def __repr__(self) -> str:
        return (
            f"Signal({self.action.value} {self.symbol} x{self.quantity:.0f}"
            f" @ ${self.price:.2f} [{self.reason.value}] "
            f"置信度:{self.confidence:.0%})"
        )


def hold(symbol: str, strategy: str = "") -> Signal:
    """空信号"""
    return Signal(action=SignalAction.HOLD, symbol=symbol, strategy=strategy)


def buy(
    symbol: str,
    quantity: float,
    price: Optional[float] = None,
    reason: SignalReason = SignalReason.MANUAL,
    confidence: float = 0.5,
    strategy: str = "",
    **metadata,
) -> Signal:
    """买入信号"""
    return Signal(
        action=SignalAction.BUY,
        symbol=symbol,
        quantity=quantity,
        price=price,
        reason=reason,
        confidence=confidence,
        strategy=strategy,
        metadata=metadata,
    )


def sell(
    symbol: str,
    quantity: float,
    price: Optional[float] = None,
    reason: SignalReason = SignalReason.MANUAL,
    confidence: float = 0.5,
    strategy: str = "",
    **metadata,
) -> Signal:
    """卖出信号"""
    return Signal(
        action=SignalAction.SELL,
        symbol=symbol,
        quantity=quantity,
        price=price,
        reason=reason,
        confidence=confidence,
        strategy=strategy,
        metadata=metadata,
    )
