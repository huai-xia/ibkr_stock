"""
绩效分析模块
计算夏普比率、最大回撤、Calmar 比率、胜率、盈亏比等
"""

import logging
from typing import Optional

import pandas as pd
import numpy as np

from src.backtest.engine import BacktestResult

logger = logging.getLogger(__name__)


class PerformanceAnalyzer:
    """
    绩效分析器

    使用方法:
        analyzer = PerformanceAnalyzer()
        result = analyzer.analyze(backtest_result)
    """

    @staticmethod
    def analyze(result: BacktestResult, risk_free_rate: float = 0.02) -> BacktestResult:
        """
        计算所有绩效指标并回填到 BacktestResult

        Args:
            result: 回测结果
            risk_free_rate: 无风险利率（默认 2%）

        Returns:
            填充了绩效指标的 BacktestResult
        """
        # CAGR 年化收益率
        result.cagr = PerformanceAnalyzer.cagr(result)

        # 最大回撤
        result.max_drawdown = PerformanceAnalyzer.max_drawdown(result)

        # 夏普比率
        result.sharpe = PerformanceAnalyzer.sharpe_ratio(result, risk_free_rate)

        # Calmar 比率
        result.calmar = PerformanceAnalyzer.calmar_ratio(result)

        # 胜率
        result.win_rate = PerformanceAnalyzer.win_rate(result)

        # 盈亏比
        result.profit_factor = PerformanceAnalyzer.profit_factor(result)

        return result

    @staticmethod
    def cagr(result: BacktestResult) -> float:
        """年化复合增长率"""
        if result.equity_curve is None or len(result.equity_curve) < 2:
            return 0.0

        equity = result.equity_curve["equity"]
        days = _count_trading_days(result)
        if days <= 0:
            return 0.0

        total_return = equity.iloc[-1] / equity.iloc[0]
        years = days / 252

        if years <= 0:
            return 0.0

        return round(total_return ** (1 / years) - 1, 4)

    @staticmethod
    def max_drawdown(result: BacktestResult) -> float:
        """最大回撤（百分比）"""
        if result.equity_curve is None or len(result.equity_curve) < 2:
            return 0.0

        equity = result.equity_curve["equity"]
        running_max = equity.cummax()
        drawdown = (equity - running_max) / running_max
        return round(drawdown.min(), 4)  # 负数，如 -0.25 表示 25% 回撤

    @staticmethod
    def sharpe_ratio(result: BacktestResult, risk_free_rate: float = 0.02) -> float:
        """夏普比率"""
        if result.daily_returns is None or len(result.daily_returns) < 2:
            return 0.0

        daily_rf = risk_free_rate / 252
        excess = result.daily_returns - daily_rf

        if excess.std() == 0:
            return 0.0

        # 年化
        return round(float(excess.mean() / excess.std() * np.sqrt(252)), 2)

    @staticmethod
    def calmar_ratio(result: BacktestResult) -> float:
        """Calmar 比率 = CAGR / |最大回撤|"""
        cagr = result.cagr or PerformanceAnalyzer.cagr(result)
        mdd = abs(result.max_drawdown or PerformanceAnalyzer.max_drawdown(result))
        if mdd == 0:
            return 0.0
        return round(cagr / mdd, 2)

    @staticmethod
    def win_rate(result: BacktestResult) -> float:
        """胜率"""
        if not result.trades:
            return 0.0
        wins = sum(1 for t in result.trades if t.pnl > 0)
        return round(wins / len(result.trades), 4)

    @staticmethod
    def profit_factor(result: BacktestResult) -> float:
        """盈亏比 = 总盈利 / 总亏损"""
        if not result.trades:
            return 0.0
        gross_profit = sum(t.pnl for t in result.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in result.trades if t.pnl < 0))
        if gross_loss == 0:
            return gross_profit if gross_profit > 0 else 0.0
        return round(gross_profit / gross_loss, 2)

    @staticmethod
    def summary(result: BacktestResult) -> str:
        """生成绩效摘要字符串"""
        mdd_pct = abs(result.max_drawdown) * 100
        total_return_pct = result.total_return_pct * 100
        cagr_pct = result.cagr * 100

        lines = [
            "┌─────────────────────────────────────────┐",
            f"│  回测绩效报告 — {result.strategy_name:<20s}    │",
            "├─────────────────────────────────────────┤",
            f"│  时间范围: {result.start_date[:10]} ~ {result.end_date[:10]}      │",
            f"│  初始资金: ${result.initial_capital:>12,.2f}               │",
            f"│  最终资金: ${result.final_equity:>12,.2f}               │",
            f"│  总收益率: {total_return_pct:>11.2f}%               │",
            f"│  年化收益 (CAGR): {cagr_pct:>5.2f}%               │",
            "├─────────────────────────────────────────┤",
            f"│  夏普比率: {result.sharpe:>12.2f}               │",
            f"│  最大回撤: {mdd_pct:>11.2f}%               │",
            f"│  Calmar:   {result.calmar:>12.2f}               │",
            "├─────────────────────────────────────────┤",
            f"│  总交易数: {result.total_trades:>11d}               │",
            f"│  胜率:     {result.win_rate:>11.1%}               │",
            f"│  盈亏比:   {result.profit_factor:>11.2f}               │",
            "└─────────────────────────────────────────┘",
        ]
        return "\n".join(lines)


def _count_trading_days(result: BacktestResult) -> int:
    """估算交易日数"""
    if result.equity_curve is None:
        return 0
    return len(result.equity_curve)
