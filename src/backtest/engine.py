"""
回测引擎
基于历史数据模拟策略执行

流程:
    历史数据 → 逐根K线喂给策略 → 模拟成交 → 记录每笔交易 → 输出绩效
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

from src.strategy.base import Strategy, StrategyContext
from src.strategy.signals import Signal, SignalAction
from src.strategy.indicators import Indicators, add_all

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """回测中的单笔交易"""
    entry_time: str
    exit_time: str
    symbol: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    entry_reason: str
    exit_reason: str
    holding_bars: int


@dataclass
class BacktestResult:
    """回测结果"""
    symbol: str
    strategy_name: str
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    total_return: float
    total_return_pct: float
    # 绩效指标（调用 performance.py 填充）
    cagr: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    # 详细数据
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: pd.DataFrame = None
    daily_returns: pd.Series = None


class BacktestEngine:
    """
    回测引擎

    使用方法:
        engine = BacktestEngine(strategy, initial_capital=10000)
        result = engine.run(df)   # df 含 OHLCV
        print(result.total_return_pct)
    """

    def __init__(
        self,
        strategy: Strategy,
        initial_capital: float = 10000.0,
        commission: float = 0.001,  # 0.1% 佣金
        slippage: float = 0.0005,   # 0.05% 滑点
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        self._indicators = Indicators()

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """
        执行回测

        Args:
            df: OHLCV 数据（需含 open/high/low/close/volume）

        Returns:
            BacktestResult
        """
        # 1. 添加技术指标
        df = self._indicators.add_all(df)

        # 2. 添加前值列（用于判断均线交叉）
        for col in df.columns:
            if col.startswith("sma_") or col.startswith("ema_"):
                df[f"prev_{col}"] = df[col].shift(1)

        # 3. 初始化（策略设为回测模式）
        symbol = self.strategy.name
        self.strategy.backtest_mode = True
        context = StrategyContext(
            symbol=symbol,
            initial_capital=self.initial_capital,
            cash=self.initial_capital,
        )
        self.strategy.reset(symbol)

        trades: list[BacktestTrade] = []
        equity_curve = []
        open_trade: Optional[dict] = None

        # 4. 逐根 K 线回放
        for idx, (_, bar) in enumerate(df.iterrows()):
            signal = self.strategy.next(bar, context)

            # 市值计价
            equity = context.cash + context.position * bar["close"]
            equity_curve.append({
                "date": idx,
                "equity": equity,
                "cash": context.cash,
                "position": context.position,
                "price": bar["close"],
            })

            if not signal or not signal.is_actionable:
                continue

            fill_price = self._apply_slippage(bar["close"], signal.action)

            if signal.action == SignalAction.BUY:
                if context.position > 0:
                    continue  # 已有持仓，忽略重复买入
                cost = signal.quantity * fill_price * (1 + self.commission)
                actual_qty = signal.quantity
                if cost > context.cash:
                    # 资金不足时按实际可买数量调整
                    actual_qty = int(context.cash / (fill_price * (1 + self.commission)))
                    cost = actual_qty * fill_price * (1 + self.commission)
                if actual_qty <= 0:
                    continue

                context.cash -= cost
                context.position = actual_qty
                context.avg_cost = fill_price
                open_trade = {
                    "entry_time": str(idx),
                    "entry_price": fill_price,
                    "quantity": actual_qty,
                    "entry_reason": signal.reason.value,
                }

            elif signal.action == SignalAction.SELL:
                if context.position <= 0 or open_trade is None:
                    continue  # 无持仓可平
                sell_qty = min(signal.quantity, context.position)
                proceeds = sell_qty * fill_price * (1 - self.commission)
                pnl = proceeds - sell_qty * open_trade["entry_price"]
                pnl_pct = pnl / (sell_qty * open_trade["entry_price"])

                trades.append(BacktestTrade(
                    entry_time=open_trade["entry_time"],
                    exit_time=str(idx),
                    symbol=symbol,
                    entry_price=open_trade["entry_price"],
                    exit_price=fill_price,
                    quantity=sell_qty,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 4),
                    entry_reason=open_trade["entry_reason"],
                    exit_reason=signal.reason.value,
                    holding_bars=len(equity_curve) - 1 - _find_bar_index(
                        equity_curve, open_trade["entry_time"]
                    ),
                ))

                context.cash += proceeds
                context.position = 0
                context.avg_cost = 0
                context.trade_count += 1
                open_trade = None

        # 5. 如果还有持仓，按最后价格平仓
        if context.position > 0 and open_trade:
            last_price = df.iloc[-1]["close"]
            proceeds = context.position * last_price * (1 - self.commission)
            pnl = proceeds - open_trade["quantity"] * open_trade["entry_price"]
            pnl_pct = pnl / (open_trade["quantity"] * open_trade["entry_price"])

            trades.append(BacktestTrade(
                entry_time=open_trade["entry_time"],
                exit_time=str(df.index[-1]),
                symbol=symbol,
                entry_price=open_trade["entry_price"],
                exit_price=last_price,
                quantity=open_trade["quantity"],
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 4),
                entry_reason=open_trade["entry_reason"],
                exit_reason="回测结束强制平仓",
                holding_bars=len(df) - _find_bar_index(equity_curve, open_trade["entry_time"]),
            ))

            context.cash += proceeds
            context.position = 0

        # 6. 组装结果
        final_equity = context.cash
        total_return = final_equity - self.initial_capital
        total_return_pct = total_return / self.initial_capital

        equity_df = pd.DataFrame(equity_curve)
        daily_returns = equity_df["equity"].pct_change().dropna() if len(equity_df) > 1 else pd.Series()

        result = BacktestResult(
            symbol=symbol,
            strategy_name=self.strategy.name,
            start_date=str(df.index[0]) if len(df) > 0 else "",
            end_date=str(df.index[-1]) if len(df) > 0 else "",
            initial_capital=self.initial_capital,
            final_equity=round(final_equity, 2),
            total_return=round(total_return, 2),
            total_return_pct=round(total_return_pct, 4),
            total_trades=len(trades),
            trades=trades,
            equity_curve=equity_df,
            daily_returns=daily_returns,
        )

        logger.info(
            "回测完成: %s | 初始 $%.0f → 最终 $%.0f (%.1f%%) | %d 笔交易",
            self.strategy.name, self.initial_capital,
            final_equity, total_return_pct * 100, len(trades),
        )

        return result

    def _apply_slippage(self, price: float, action: SignalAction) -> float:
        """模拟滑点"""
        if action == SignalAction.BUY:
            return price * (1 + self.slippage)
        else:
            return price * (1 - self.slippage)


def _find_bar_index(equity_curve: list, date_str: str) -> int:
    """查找 date 在 equity_curve 中的位置"""
    for i, e in enumerate(equity_curve):
        if e["date"] == date_str:
            return i
    return 0
